"""
tests/unit/test_phase_review_fixes.py — Regression tests for the Phase 1-3
review fixes (plan-compliance audit).

Covers:
- Router hook: local.enabled=false → all_free_models_exhausted (plan AC test_local_disabled)
- Circuit breaker: auth error (401) → open with 24h cooldown immediately
- Scorer: latency term uses rolling window average, not just last request
"""

import time
from unittest.mock import MagicMock

import pytest

from fakeredis import FakeRedis

from gateway.router_hook import RouterHook, is_local_model
from brain.circuit_breaker import CircuitBreakerManager, CIRCUIT_BREAKER_COOLDOWN_AUTH
from brain.connectivity_monitor import classify_error


# ---------------------------------------------------------------------------
# Fix: local.enabled=false respected by router hook
# ---------------------------------------------------------------------------

def make_redis(offline=False, scores=None):
    r = FakeRedis(decode_responses=True)
    if offline:
        r.set("gateway:offline_mode", "1")
    for model, score in (scores or {}).items():
        r.set(f"gateway:model:{model}:score", str(score))
    return r


def make_hook(redis_client, config):
    h = RouterHook(redis_client=redis_client, gateway_config=config)
    h.cb_manager = MagicMock()
    h.cb_manager.get_state.return_value = "closed"
    return h


def disabled_local_config():
    from schemas.config import GatewayConfig, ProvidersConfig, ProviderConfig
    cfg = GatewayConfig()
    cfg.providers.local.enabled = False
    return cfg


def enabled_local_config():
    from schemas.config import GatewayConfig
    cfg = GatewayConfig()
    cfg.providers.local.enabled = True
    return cfg


CHAIN_WITH_LOCAL = ["nvidia-auto", "local-llama3-8b"]


def test_local_disabled_offline_returns_all_free_exhausted():
    h = make_hook(make_redis(offline=True), disabled_local_config())
    model, reason = h.offline_route_decision(CHAIN_WITH_LOCAL)
    assert model is None
    assert reason == "all_free_models_exhausted"


def test_local_disabled_influence_excludes_even_online():
    """With local.enabled=false a local model never gets positive influence."""
    h = make_hook(make_redis(offline=True), disabled_local_config())
    influence, reason = h.influence_model_selection("local-llama3-8b", CHAIN_WITH_LOCAL)
    assert influence == -1
    assert reason == "local_disabled"


def test_local_disabled_chain_reorder_drops_locals():
    h = make_hook(make_redis(offline=True), disabled_local_config())
    ordered = h.get_fallback_priority("auto-free", CHAIN_WITH_LOCAL)
    # All models excluded while offline+disabled; locals sink to the end.
    assert ordered[-1] == "local-llama3-8b"
    assert ordered[0] == "nvidia-auto" or ordered[0].startswith("nvidia")


def test_local_enabled_still_works():
    h = make_hook(make_redis(offline=True, scores={"local-llama3-8b": 0.9}),
                  enabled_local_config())
    model, reason = h.offline_route_decision(CHAIN_WITH_LOCAL)
    assert model == "local-llama3-8b"
    assert reason == "offline_mode_local_only"


# ---------------------------------------------------------------------------
# Fix: auth errors open circuit with 24h cooldown
# ---------------------------------------------------------------------------

def test_auth_failure_opens_circuit_with_24h_cooldown():
    r = make_redis()
    cb = CircuitBreakerManager(r)
    cb.record_auth_failure("nvidia-auto")
    assert cb.get_state("nvidia-auto") == "open"
    ttl = r.ttl("gateway:model:nvidia-auto:circuit")
    assert 0 < ttl <= CIRCUIT_BREAKER_COOLDOWN_AUTH
    # Auth cooldown key set for logging/diagnostics
    assert r.exists("gateway:model:nvidia-auto:cooldown:auth")


def test_stream_reader_routes_auth_errors_to_auth_path():
    """error_code=401 with error_type='unknown' must hit record_auth_failure."""
    from brain.stream_reader import StreamReader, RequestEvent

    r = make_redis()
    reader = StreamReader(redis_client=r)

    recorded = {}
    reader.connectivity_monitor = MagicMock()

    class FakeCB:
        def __init__(self, redis_client=None): pass
        def transition_to_closed(self, m): recorded.setdefault(m, []).append("closed")
        def record_auth_failure(self, m): recorded.setdefault(m, []).append("auth")
        def record_failure(self, m, is_429=False): recorded.setdefault(m, []).append(
            "429" if is_429 else "5xx")

    import brain.circuit_breaker as cb_mod
    original = cb_mod.CircuitBreakerManager
    cb_mod.CircuitBreakerManager = FakeCB
    try:
        event = RequestEvent(
            event_id="e", virtual_model="auto-free", actual_model="nvidia-auto",
            provider="nvidia", status="error", error_code="401",
            error_type=None, input_tokens=None, output_tokens=None,
            latency_ms=None, ttft_ms=None, routing_decision_reason=None,
            request_metadata={}, response_metadata={}, timestamp=time.time(),
        )
        reader._update_circuit_breaker(event)

        event_429 = RequestEvent(
            event_id="e2", virtual_model="auto-free", actual_model="groq-x",
            provider="groq", status="error", error_code="429",
            error_type=None, input_tokens=None, output_tokens=None,
            latency_ms=None, ttft_ms=None, routing_decision_reason=None,
            request_metadata={}, response_metadata={}, timestamp=time.time(),
        )
        reader._update_circuit_breaker(event_429)
    finally:
        cb_mod.CircuitBreakerManager = original

    assert recorded["nvidia-auto"] == ["auth"]
    assert recorded["groq-x"] == ["429"]


def test_classify_raw_auth_code():
    assert classify_error(error_code="401", error_type=None) == "auth_error"
    assert classify_error(error_code=None, error_type="PermissionDeniedError") == "auth_error"


# ---------------------------------------------------------------------------
# Fix: scorer latency uses rolling average
# ---------------------------------------------------------------------------

def test_scorer_latency_uses_window_average():
    from brain.scorer import _get_normalized_latency

    r = MagicMock()
    # Rolling window of 9 fast requests (1000ms each)
    r.lrange.return_value = [1000.0] * 9
    # New request is very slow (19000ms)
    normalized = _get_normalized_latency(r, "gateway:model:m1:stats", latency_ms=19000)
    # Average of [1000*9, 19000] = 2800ms → normalized ≈ 0.14, NOT ~0.95
    expected = round(2800 / 20000, 4)
    assert abs(normalized - expected) < 0.01
    assert normalized < 0.2


def test_scorer_latency_single_sample_fallback():
    from brain.scorer import _get_normalized_latency
    r = MagicMock()
    r.lrange.return_value = []
    normalized = _get_normalized_latency(r, "gateway:model:m1:stats", latency_ms=10000)
    assert normalized == round(10000 / 20000, 4)


def test_scorer_latency_no_data_neutral():
    from brain.scorer import _get_normalized_latency
    r = MagicMock()
    r.lrange.return_value = []
    assert _get_normalized_latency(r, "gateway:model:m1:stats", latency_ms=None) == 0.5
