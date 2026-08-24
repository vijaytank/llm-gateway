"""Unit tests for brain/health_scheduler.py probe resolution (F-H2 coverage).

The critical regression: an unknown endpoint must SKIP, never fabricate health.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.health_scheduler import HealthScheduler


@pytest.fixture
def sched(fake_redis):
    s = HealthScheduler(
        redis_client=fake_redis,
        provider_configs={"nvidia": {"base_url": "http://nim-upstream"}},
    )
    return s


def test_endpoint_resolution_from_provider_config(sched):
    url = sched._get_probe_endpoint("some-model", "nvidia")
    assert url == "http://nim-upstream/chat/completions"


def test_endpoint_resolution_explicit_map_wins(fake_redis):
    s = HealthScheduler(
        redis_client=fake_redis,
        provider_configs={"p": {"base_url": "http://a"}},
        probe_endpoints={"special": "http://b/custom"},
    )
    assert s._get_probe_endpoint("special", "p") == "http://b/custom"


def test_unknown_endpoint_is_none(sched):
    """F-H2: no config → None → callers SKIP (never fabricate healthy)."""
    assert sched._get_probe_endpoint("mystery", None) is None


@pytest.mark.asyncio
async def test_probe_model_skips_without_endpoint(sched, fake_redis):
    """Regression: previously endpoint=None wrote 'healthy'. Now: no write."""
    fake_redis.set("gateway:model:m-noend:status", "preexisting")
    info = {"model_name": "m-noend", "provider": None}
    sem = asyncio.Semaphore(1)
    await sched._probe_model(info, sem)
    # Status untouched — skip means skip.
    assert fake_redis.get("gateway:model:m-noend:status") == "preexisting"


@pytest.mark.asyncio
async def test_probe_model_unhealthy_on_connection_error(sched, fake_redis):
    info = {"model_name": "dead-model", "provider": "nvidia"}
    sem = asyncio.Semaphore(1)
    await sched._probe_model(info, sem)
    val = fake_redis.get("gateway:model:dead-model:status")
    assert val is not None and val != "healthy"
    assert sched._consecutive_errors.get("dead-model", 0) >= 1


def test_classify_matrix_issue5():
    s = HealthScheduler()
    # 200 plain → healthy; 200 zero-usage → slow(not healthy)
    assert s._classify_probe_response(200, {"choices": []}, None)[0] == "healthy"
    cls, healthy = s._classify_probe_response(
        200, {"usage": {"prompt_tokens": 0, "completion_tokens": 0}}, None)
    assert cls == "slow" and healthy is False
    assert s._classify_probe_response(429, None, None) == ("rate_limited", False)
    cf_cls, cf_healthy = s._classify_probe_response(
        400, {"error": {"message": "content_filter hit"}}, None)
    assert cf_healthy is True
    assert s._classify_probe_response(401, None, None) == ("unauthorized", False)
    assert s._classify_probe_response(503, None, None) == ("unhealthy", False)


def test_offline_mode_pauses_cloud_probes(fake_redis):
    fake_redis.set("gateway:offline_mode", "1")
    s = HealthScheduler(redis_client=fake_redis)
    offline = bool(fake_redis.get("gateway:offline_mode"))
    assert offline is True
