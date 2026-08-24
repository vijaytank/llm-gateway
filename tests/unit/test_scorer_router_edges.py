"""Extra edge coverage: scorer quota fallbacks + router hook exclusion (F-M14)."""

import pytest

from brain import scorer
from gateway.router_hook import RouterHook


def test_quota_headroom_no_redis_returns_full():
    assert scorer._get_quota_headroom(None, "m") == 1.0


def test_success_rate_no_redis_neutral():
    assert scorer._get_success_rate(None, "m", 50) == 0.5


def test_get_fallback_priority_drops_excluded(fake_redis):
    """F-M14 regression: open-circuit models are dropped, not appended."""
    fake_redis.set("gateway:model:m-open:circuit", "open")
    h = RouterHook.__new__(RouterHook)
    h.redis = fake_redis
    from brain.circuit_breaker import CircuitBreakerManager
    h.cb_manager = CircuitBreakerManager(fake_redis)
    from schemas.config import create_default_config
    h.gateway_config = create_default_config()
    h.defaults = h.gateway_config.routing_defaults
    h.local_enabled = False
    out = h.get_fallback_priority(["m-open", "m-unknown"], ["m-open", "m-unknown"])
    # Excluded model must NOT appear at the tail anymore.
    assert "m-open" not in [m for m in out]
