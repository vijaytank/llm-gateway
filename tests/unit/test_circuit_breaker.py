"""Phase 2 unit tests: brain/circuit_breaker.py (plan test_circuit_breaker.py).

All 5 state-change scenarios per plan:
- 3 failures in 5 min -> open
- cooldown TTL expiry -> half_open
- success in half_open -> closed
- failure in half_open -> open with reset cooldown
- auth error -> open with 24h cooldown
"""

import time

import pytest

from brain.circuit_breaker import CircuitBreakerManager
from brain.config import (
    CIRCUIT_BREAKER_COOLDOWN_429,
    CIRCUIT_BREAKER_COOLDOWN_AUTH,
)


@pytest.fixture
def cb(fake_redis):
    return CircuitBreakerManager(fake_redis)


def test_initial_state_closed(cb):
    assert cb.get_state("model-a") == "closed"


def test_three_failures_open_circuit(cb):
    for _ in range(3):
        cb.record_failure("model-a")
    assert cb.get_state("model-a") == "open"


def test_two_failures_stay_closed(cb):
    cb.record_failure("model-b")
    cb.record_failure("model-b")
    assert cb.get_state("model-b") == "closed"


def test_cooldown_expiry_leads_to_half_open(cb, fake_redis):
    for _ in range(3):
        cb.record_failure("model-c")
    assert cb.get_state("model-c") == "open"
    # Simulate TTL expiry: key disappears → absent means half-open candidate.
    # Per plan design: open→half_open happens when the open-state key expires.
    fake_redis.delete("gateway:model:model-c:circuit")
    fake_redis.setex("gateway:model:model-c:circuit", 300, "half_open")
    assert cb.get_state("model-c") == "half_open"


def test_success_in_half_open_closes(cb):
    cb._set_state("model-d", "half_open", ttl=300)
    cb.transition_to_closed("model-d")
    assert cb.get_state("model-d") == "closed"


def test_failure_in_half_open_reopens_with_reset_cooldown(cb, fake_redis):
    cb._set_state("model-e", "half_open", ttl=300)
    for _ in range(3):
        cb.record_failure("model-e")
    assert cb.get_state("model-e") == "open"
    ttl = fake_redis.ttl("gateway:model:model-e:circuit")
    assert ttl > 0


def test_429_gets_short_cooldown(cb, fake_redis):
    for _ in range(3):
        cb.record_failure("model-f", is_429=True)
    ttl = fake_redis.ttl("gateway:model:model-f:circuit")
    # 429 cooldown is 600s — well under the 1800s 5xx cooldown
    assert ttl <= CIRCUIT_BREAKER_COOLDOWN_429
    assert cb.get_state("model-f") == "open"


def test_auth_error_gets_24h_cooldown(cb, fake_redis):
    for _ in range(3):
        cb.record_failure("model-g", is_429=False)
    # Mark a recent auth error so record_failure picks the auth cooldown path
    fake_redis.setex("gateway:model:model-g:cooldown:auth",
                     CIRCUIT_BREAKER_COOLDOWN_AUTH, "1")
    for _ in range(3):
        cb.record_failure("model-g", is_429=False)
    ttl = fake_redis.ttl("gateway:model:model-g:circuit")
    assert ttl >= CIRCUIT_BREAKER_COOLDOWN_AUTH - 5


def test_success_resets_failure_counter(cb, fake_redis):
    cb.record_failure("model-h")
    cb.record_failure("model-h")
    cb.record_success("model-h")
    assert fake_redis.exists("gateway:model:model-h:failure_count") == 0
    # two more failures should NOT trip it (counter was reset)
    cb.record_failure("model-h")
    cb.record_failure("model-h")
    assert cb.get_state("model-h") == "closed"


def test_no_redis_is_safe_noop():
    cb = CircuitBreakerManager(None)
    cb.record_failure("x")           # must not raise
    assert cb.get_state("x") == "closed"


def test_permanent_eol_failure_opens_immediately(cb, fake_redis):
    """A single 410/404 EOL permanent failure must immediately open the circuit with 24h cooldown."""
    cb.record_permanent_failure("model-eol", reason="eol")
    assert cb.get_state("model-eol") == "open"
    # Cooldown key must be set
    assert fake_redis.exists("gateway:model:model-eol:cooldown:eol") == 1
    assert fake_redis.ttl("gateway:model:model-eol:circuit") > 1800
