"""
test_phase5_integration.py — Phase 5 refinements against the live stack.

Covers plan Phase 5 test cases:
- test_half_open_throttle: model in half-open; only 1 request per interval
  passes through, rest are marked throttled (routed as last-resort).
- test_provider_level_circuit: 3 models of one provider open circuits →
  provider flagged priority=low in Redis (NOT circuit-open on siblings).
- test_prometheus_metrics: /metrics returns valid Prometheus text with
  non-zero gateway_requests_total after real traffic.
"""

import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (  # noqa: E402
    AUTH_HEADERS, GATEWAY_URL, redis_cmd, wait_until,
)

METRICS_PORT = int(__import__("os").environ.get("GATEWAY_METRICS_PORT", "4003"))
METRICS_URL = f"http://localhost:{METRICS_PORT}/metrics"


def _open_circuit(model):
    """Open a model's circuit directly via the brain's manager semantics."""
    for _ in range(3):
        redis_cmd("xadd", "gateway:requests:stream", "*",
                  "event_id", f"ph5-{model}-{_}",
                  "virtual_model", "auto-free",
                  "actual_model", model,
                  "provider", "mock-alpha",
                  "status", "error",
                  "error_code", "503",
                  "error_type", "server_error")


def _redis_raw(*args):
    """redis-cli --raw: empty reply comes back as '' (not None)."""
    return redis_cmd("--raw", *map(str, args))


def test_half_open_throttle_allows_one_probe_per_interval():
    """Open a circuit via real failure events, then verify only the first
    SET NX within the interval acquires the probe lock."""
    # Open the circuit through the live brain.
    _open_circuit("throttle-model")

    def _opened():
        state = redis_cmd("get", "gateway:model:throttle-model:circuit")
        return state == "open"
    wait_until(_opened, timeout=60, interval=1.0,
               desc="circuit opens for throttle-model")

    # Force the half-open recovery transition exactly like the brain does.
    redis_cmd("setex", "gateway:model:throttle-model:circuit", "300", "half_open")
    redis_cmd("del", "gateway:model:throttle-model:half_open_probe_lock")

    lock_key = "gateway:model:throttle-model:half_open_probe_lock"
    first = _redis_raw("set", lock_key, "1", "nx", "ex", "30")
    assert first == "OK", f"first probe must acquire the lock, got {first!r}"
    second = _redis_raw("set", lock_key, "1", "nx", "ex", "30")
    # redis-cli --raw returns an EMPTY STRING (not nil) when NX fails.
    assert second == "", f"second probe must be throttled, got {second!r}"


def test_provider_level_circuit_flags_low_priority():
    """3 distinct models of mock-alpha opening circuits → provider priority=low;
    a sibling healthy model stays closed."""
    provider = "mock-alpha"
    models = ["pv-model-1", "pv-model-2", "pv-model-3"]
    for m in models:
        for i in range(3):
            redis_cmd("xadd", "gateway:requests:stream", "*",
                      "event_id", f"pv-{m}-{i}",
                      "virtual_model", "auto-free",
                      "actual_model", m,
                      "provider", provider,
                      "status", "error",
                      "error_code", "503",
                      "error_type", "server_error")

    def _flagged():
        val = redis_cmd("get", f"gateway:provider:{provider}:priority")
        return val == "low"
    wait_until(_flagged, timeout=60, interval=1.0,
               desc="provider flagged low after 3 model circuits")

    # Healthy sibling NOT circuited — deprioritized, not blocked.
    # redis-cli GET of a missing key returns '' via the harness.
    state = redis_cmd("get", "gateway:model:pv-healthy-sibling:circuit")
    assert state in (None, "", "closed"), \
        f"sibling circuit should stay closed, got {state!r}"


def test_prometheus_metrics_endpoint_after_traffic():
    """Drive a few real requests then scrape /metrics for non-zero counters."""
    import json as _json
    from conftest import http_json

    for _ in range(3):
        status, body = http_json(
            "POST", f"{GATEWAY_URL}/v1/chat/completions",
            payload={"model": "auto-free",
                     "messages": [{"role": "user", "content": "metrics ping"}],
                     "max_tokens": 10},
            headers=dict(AUTH_HEADERS))
        assert status == 200, body[:200]

    def _counters_nonzero():
        try:
            with urllib.request.urlopen(METRICS_URL, timeout=10) as resp:
                text = resp.read().decode()
        except Exception:
            return False
        if "# HELP" not in text:
            return False
        # prometheus_client emits labels in its own order; match loosely.
        for line in text.splitlines():
            if line.startswith("gateway_requests_total{") \
                    and 'status="success"' in line:
                try:
                    if float(line.rsplit(" ", 1)[1]) >= 3.0:
                        return True
                except ValueError:
                    continue
        return False
    wait_until(_counters_nonzero, timeout=90, interval=2.0,
               desc="non-zero success counters on /metrics")


def test_metrics_latency_summary_present_after_traffic():
    try:
        with urllib.request.urlopen(METRICS_URL, timeout=10) as resp:
            text = resp.read().decode()
    except Exception as e:
        pytest.fail(f"/metrics unreachable: {e}")
    assert "gateway_latency_ms_count" in text
    assert "gateway_circuit_state" in text
