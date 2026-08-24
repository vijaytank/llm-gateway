"""Unit tests for gateway/health_startup.py — review F-M15 / F-L3 coverage.

Verifies the deduplicated wave runner: wave partitioning, classification
persistence to Redis, and stagger timing helpers.
"""

import asyncio
import fakeredis

import pytest

from gateway import health_startup as hs


@pytest.fixture
def rclient():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_wave_partition(rclient):
    providers = [
        {"name": "nvidia", "base_url": "http://n"},
        {"name": "groq", "base_url": "http://g"},
        {"name": "cerebras", "base_url": "http://c"},
        {"name": "openrouter", "base_url": "http://o"},
        {"name": "custom-x", "base_url": "http://x"},  # unassigned → wave 3
    ]
    w1 = [p["name"] for p in hs._providers_for_wave(providers, 1)]
    w2 = [p["name"] for p in hs._providers_for_wave(providers, 2)]
    w3 = [p["name"] for p in hs._providers_for_wave(providers, 3)]
    assert w1 == ["nvidia", "groq"]
    assert w2 == ["cerebras", "openrouter"]
    assert w3 == ["custom-x"]
    # Every provider is covered exactly once across waves.
    assert sorted(w1 + w2 + w3) == sorted(p["name"] for p in providers)


def test_classify_response_issue4_matrix():
    # Plan Issue 4 matrix: 200 healthy (plain), 429 rate_limited,
    # 400 content_filter → healthy, 401 unauthorized, 503 unhealthy.
    assert hs.classify_response(200, {"choices": []}) == "healthy"
    # 200 with explicit zero-usage body = model loaded but empty → "slow"
    zero_usage = {"usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    assert hs.classify_response(200, zero_usage) == "slow"
    assert hs.classify_response(429, None) == "rate_limited"
    assert hs.classify_response(
        400, {"error": {"message": "content_filter triggered"}}) == "healthy"
    assert hs.classify_response(
        400, {"error": {"type": "invalid_request_error"}}) == "misconfigured"
    assert hs.classify_response(401, None) == "unauthorized"
    assert hs.classify_response(503, None) == "unhealthy"


def test_determine_wave_at_time():
    assert hs.determine_wave_at_time(0) == 1
    assert hs.determine_wave_at_time(29) == 1
    assert hs.determine_wave_at_time(30) == 2
    assert hs.determine_wave_at_time(59) == 2
    assert hs.determine_wave_at_time(60) == 3


@pytest.mark.asyncio
async def test_run_wave_persists_status(rclient, monkeypatch):
    async def fake_probe(client, base_url, name):
        if name == "good":
            return hs.ProbeResult("good", True, "healthy", 200)
        return hs.ProbeResult("bad", False, "unhealthy", 503)

    monkeypatch.setattr(hs, "probe_provider", fake_probe)
    results = await hs._run_wave(
        None,
        [{"name": "good", "base_url": "http://g"},
         {"name": "bad", "base_url": "http://b"}],
        rclient,
        "test wave")
    assert results == {"good": "healthy", "bad": "unhealthy"}
    assert rclient.get("gateway:model:good:status") == "healthy"
    assert rclient.get("gateway:model:bad:status") == "unhealthy"
    # TTLs set so stale status expires
    assert rclient.ttl("gateway:model:good:status") > 0


@pytest.mark.asyncio
async def test_run_health_checks_partitions_providers(rclient, monkeypatch):
    """Each provider is probed exactly once across the full sequence."""
    probed = []

    async def fake_probe(client, base_url, name):
        probed.append(name)
        return hs.ProbeResult(name, True, "healthy", 200)

    monkeypatch.setattr(hs, "probe_provider", fake_probe)
    providers = [
        {"name": "nvidia", "base_url": "http://n"},
        {"name": "cerebras", "base_url": "http://c"},
        {"name": "leftover", "base_url": "http://l"},
    ]
    results = await hs.run_health_checks(providers, rclient)
    assert sorted(probed) == ["cerebras", "leftover", "nvidia"]
    assert results["nvidia"] == "healthy"
    assert rclient.get("gateway:model:leftover:status") == "healthy"
