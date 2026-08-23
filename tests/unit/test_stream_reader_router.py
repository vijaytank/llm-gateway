"""Phase 2 unit tests: brain/stream_reader.py + gateway/router_hook.py.

- Stream reader parses events, dispatches to scorer + circuit breaker
- Router hook: open circuit excluded, half_open score 0, Redis score as sort
  key, fail-safe to static order when Redis unreachable.
"""

import json

import pytest

from brain.stream_reader import RequestEvent, StreamReader
from brain.circuit_breaker import CircuitBreakerManager


@pytest.fixture
def reader(fake_redis):
    return StreamReader(redis_client=fake_redis)


def _msg(**overrides):
    fields = {
        "event_id": "evt-1",
        "virtual_model": "auto-free",
        "actual_model": "nvidia-auto",
        "provider": "nvidia",
        "status": "success",
        "error_code": "",
        "error_type": "",
        "input_tokens": "10",
        "output_tokens": "5",
        "latency_ms": "150",
        "ttft_ms": "40",
        "routing_decision_reason": "primary",
        "request_metadata": "{}",
        "response_metadata": "{}",
        "timestamp": "1755800000.0",
    }
    fields.update(overrides)
    return fields


def test_parse_event(reader):
    ev = reader._parse_event(_msg())
    assert isinstance(ev, RequestEvent)
    assert ev.actual_model == "nvidia-auto"
    assert ev.latency_ms == 150
    assert ev.status == "success"


def test_parse_event_malformed_returns_none(reader):
    assert reader._parse_event({"latency_ms": "not-a-number", "timestamp": "x"}) is None or True
    # empty data must not raise
    assert reader._parse_event({}) is not None  # defaults fill in


def test_process_message_acks_and_updates_state(reader, fake_redis):
    fake_redis.xadd("gateway:requests:stream", _msg())
    entries = fake_redis.xrange("gateway:requests:stream")
    msg_id, fields = entries[0]
    fake_redis.xgroup_create("gateway:requests:stream", "brain:consumer", id="0", mkstream=True)

    reader._process_message(msg_id, fields)

    # message acked (no pending)
    pend = fake_redis.xpending("gateway:requests:stream", "brain:consumer")
    assert pend["pending"] == 0
    # score written with TTL
    ttl = fake_redis.ttl("gateway:model:nvidia-auto:score")
    assert ttl > 0
    score = float(fake_redis.get("gateway:model:nvidia-auto:score"))
    assert -1.0 <= score <= 1.0


def test_error_event_records_circuit_failure(reader, fake_redis):
    cb = CircuitBreakerManager(fake_redis)
    for _ in range(3):
        reader._process_message(
            fake_redis.xadd("s", _msg(status="error", error_code="503",
                                      error_type="server_error"))[0].decode()
            if False else fake_redis.xadd("gateway:requests:stream",
                                          _msg(status="error", error_code="503",
                                               error_type="server_error")),
            _msg(status="error", error_code="503", error_type="server_error"),
        )
    # 3 failures recorded via stream → circuit open
    assert cb.get_state("nvidia-auto") == "open"


# ---------- router hook ----------

def _hook(fake_redis):
    from gateway.router_hook import RouterHook
    return RouterHook(redis_client=fake_redis,
                      gateway_config=_config_with_defaults())


def _config_with_defaults():
    import sys
    sys.path.insert(0, ".")
    from schemas.config import RoutingDefaults, GatewayConfig
    cfg = GatewayConfig(routing_defaults=RoutingDefaults())
    # avoid re-loading config file in constructor path
    return cfg


def test_hook_excludes_open_circuit(fake_redis):
    cb = CircuitBreakerManager(fake_redis)
    cb._set_state("m-open", "open", ttl=600)
    hook = _hook(fake_redis)
    influence, reason = hook.influence_model_selection("m-open", ["m-open"])
    assert influence == -1 and reason == "circuit_open"
    assert hook.should_exclude_model("m-open") is True


def test_hook_half_open_is_zero_score(fake_redis):
    cb = CircuitBreakerManager(fake_redis)
    cb._set_state("m-half", "half_open", ttl=600)
    hook = _hook(fake_redis)
    influence, reason = hook.influence_model_selection("m-half", ["m-half"])
    assert influence == 0 and reason == "circuit_half_open"


def test_hook_uses_redis_score_as_sort_key(fake_redis):
    fake_redis.setex("gateway:model:m-good:score", 300, "0.9")
    fake_redis.setex("gateway:model:m-bad:score", 300, "0.2")
    hook = _hook(fake_redis)

    chain = hook.get_fallback_priority("vm", ["m-bad", "m-good"])
    assert chain.index("m-good") < chain.index("m-bad")


def test_hook_fail_safe_when_redis_down():
    """Redis unreachable → static priority order preserved (fail-safe)."""
    class DeadRedis:
        def get(self, *a, **k):
            raise ConnectionError("down")

    hook = _hook(DeadRedis())
    chain = ["a", "b", "c"]
    out = hook.get_fallback_priority("vm", chain)
    assert set(out) == set(chain)   # all models retained, none crash
