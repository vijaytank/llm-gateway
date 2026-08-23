"""
tests/unit/test_router_hook_offline.py — Phase 3 router offline gating.

Covers:
- offline → cloud models excluded (influence -1)
- offline + local in chain → local routed, highest score first
- offline + no local models → offline_no_local_models signal
- online behavior unchanged
"""

from unittest.mock import MagicMock

import pytest

from gateway.router_hook import RouterHook, is_local_model


def make_redis(offline=False, scores=None):
    r = MagicMock()

    def get(key):
        key = key.decode() if isinstance(key, bytes) else key
        if key == "gateway:offline_mode":
            return b"1" if offline else None
        if scores and key.startswith("gateway:model:") and key.endswith(":score"):
            model = key.split(":")[2]
            if model in scores:
                return str(scores[model]).encode()
        return None

    r.get.side_effect = get
    return r


def make_hook(redis_client):
    from schemas.config import GatewayConfig
    h = RouterHook.__new__(RouterHook)
    h.redis = redis_client
    h.cb_manager = MagicMock()
    h.cb_manager.get_state.return_value = "closed"
    h.gateway_config = GatewayConfig()
    h.gateway_config.providers.local.enabled = True  # tests here assume local enabled
    h.defaults = h.gateway_config.routing_defaults
    h.local_enabled = True
    return h


CHAIN = ["nvidia-auto", "groq-auto-free", "local-llama3-8b", "openrouter-free"]


def test_is_local_model():
    assert is_local_model("local-llama3-8b")
    assert is_local_model("LOCAL-mistral")
    assert not is_local_model("nvidia-auto")
    assert not is_local_model("")


def test_offline_excludes_cloud_models():
    h = make_hook(make_redis(offline=True))
    influence, reason = h.influence_model_selection("nvidia-auto", CHAIN)
    assert influence == -1
    assert reason == "offline_mode_cloud_excluded"


def test_offline_allows_local_model():
    h = make_hook(make_redis(offline=True, scores={"local-llama3-8b": 0.9}))
    influence, reason = h.influence_model_selection("local-llama3-8b", CHAIN)
    assert influence > 0
    assert reason != "offline_mode_cloud_excluded"


def test_online_behavior_unchanged():
    h = make_hook(make_redis(offline=False, scores={"nvidia-auto": 0.9}))
    influence, reason = h.influence_model_selection("nvidia-auto", CHAIN)
    assert influence > 0
    assert reason == "high_score"


def test_offline_route_decision_prefers_local():
    h = make_hook(make_redis(offline=True, scores={"local-a": 0.5, "local-b": 0.9}))
    model, reason = h.offline_route_decision(
        ["nvidia-auto", "local-a", "local-b"]
    )
    assert model == "local-b"
    assert reason == "offline_mode_local_only"


def test_offline_route_decision_open_circuit_local_skipped():
    h = make_hook(make_redis(offline=True, scores={"local-a": 0.9, "local-b": 0.4}))
    h.cb_manager.get_state.side_effect = lambda m: "open" if m == "local-a" else "closed"
    model, reason = h.offline_route_decision(["local-a", "local-b"])
    assert model == "local-b"


def test_offline_no_local_models():
    h = make_hook(make_redis(offline=True))
    model, reason = h.offline_route_decision(["nvidia-auto", "groq-auto-free"])
    assert model is None
    assert reason == "offline_no_local_models"


def test_offline_chain_reorder_puts_locals_first():
    h = make_hook(make_redis(offline=True, scores={"local-llama3-8b": 0.7}))
    ordered = h.get_fallback_priority("auto-free", CHAIN)
    assert ordered[0].startswith("local-")
    # All cloud models still present after locals
    rest = ordered[1:]
    assert set(rest) == {"nvidia-auto", "groq-auto-free", "openrouter-free"}


def test_redis_down_fail_open_not_offline():
    """Redis unreachable → fail-open: model stays routable (last resort), never
    hard-excluded, so static fallback chain order still applies."""
    r = MagicMock()
    r.get.side_effect = ConnectionError("redis down")
    h = make_hook(r)
    assert h.is_offline() is False
    influence, reason = h.influence_model_selection("nvidia-auto", CHAIN)
    assert influence == 0
    assert reason.startswith("error:")
