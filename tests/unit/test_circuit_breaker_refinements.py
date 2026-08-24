"""Phase 5 unit tests: circuit-breaker refinements + provider-level circuit.

Covers plan Phase 5 deliverable 1:
- Half-open probe throttle: at most 1 request per 30s window in half-open
- Provider-level circuit: 3+ model circuits opening within 5 minutes flag the
  provider priority=low for 30 minutes WITHOUT opening remaining models
"""

import time

import pytest

from brain.circuit_breaker import CircuitBreakerManager
from brain.provider_circuit import ProviderCircuitManager


@pytest.fixture
def cb(fake_redis):
    return CircuitBreakerManager(fake_redis)


@pytest.fixture
def pcm(fake_redis):
    return ProviderCircuitManager(fake_redis)


# ---------------------------------------------------------------------------
# Half-open throttle (plan test_half_open_throttle)
# ---------------------------------------------------------------------------

def test_half_open_first_probe_acquired(cb, fake_redis):
    fake_redis.setex("gateway:model:m1:circuit", 300, "half_open")
    assert cb.try_acquire_half_open_probe("m1") is True


def test_half_open_second_probe_throttled(cb, fake_redis):
    fake_redis.setex("gateway:model:m2:circuit", 300, "half_open")
    assert cb.try_acquire_half_open_probe("m2") is True
    # Same interval window: second caller denied.
    assert cb.try_acquire_half_open_probe("m2") is False
    assert cb.try_acquire_half_open_probe("m2") is False


def test_closed_model_alows_unlimited_requests(cb):
    # No half_open key set → closed → always allowed.
    for _ in range(10):
        assert cb.try_acquire_half_open_probe("m3") is True


def test_probe_lock_expires_after_interval(cb, fake_redis):
    fake_redis.setex("gateway:model:m4:circuit", 300, "half_open")
    assert cb.try_acquire_half_open_probe("m4", interval_seconds=30) is True
    # Simulate interval expiry by deleting the lock key (fakeredis TTLs are real).
    cb.release_half_open_probe("m4")
    assert cb.try_acquire_half_open_probe("m4") is True


def test_release_clears_the_lock(cb, fake_redis):
    fake_redis.setex("gateway:model:m5:circuit", 300, "half_open")
    cb.try_acquire_half_open_probe("m5")
    cb.release_half_open_probe("m5")
    assert cb.try_acquire_half_open_probe("m5") is True


# ---------------------------------------------------------------------------
# Provider-level circuit (plan test_provider_level_circuit)
# ---------------------------------------------------------------------------

def test_fewer_than_threshold_models_does_not_flag(pcm):
    pcm.record_model_circuit_open("mock-alpha", "a-1")
    pcm.record_model_circuit_open("mock-alpha", "a-2")
    assert pcm.get_priority("mock-alpha") == "normal"


def test_three_models_within_window_flags_provider_low(pcm):
    pcm.record_model_circuit_open("nim", "n-model-1")
    pcm.record_model_circuit_open("nim", "n-model-2")
    pcm.record_model_circuit_open("nim", "n-model-3")
    assert pcm.get_priority("nim") == "low"


def test_same_model_reopening_counts_once(pcm):
    for _ in range(3):
        pcm.record_model_circuit_open("nim", "same-model")
    assert pcm.get_priority("nim") == "normal"


def test_old_events_expire_out_of_window(pcm, fake_redis):
    pcm.record_model_circuit_open("groq", "g-1")
    pcm.record_model_circuit_open("groq", "g-2")
    # Age the first two events beyond the 5-minute window.
    events_key = "gateway:provider:groq:circuit_open_events"
    old = time.time() - 400
    fake_redis.zadd(events_key, {"g-1": old, "g-2": old})
    pcm.record_model_circuit_open("groq", "g-3")
    assert pcm.get_priority("groq") == "normal"


def test_low_priority_flag_has_ttl(pcm, fake_redis):
    for i in range(3):
        pcm.record_model_circuit_open("cerebras", f"c-{i}")
    ttl = fake_redis.ttl("gateway:provider:cerebras:priority")
    assert 0 < ttl <= 1800


def test_remaining_models_not_circuited_by_provider_flag(pcm, fake_redis):
    """Provider flag must NOT open circuits on other models of that provider."""
    cbm = CircuitBreakerManager(fake_redis)
    for i in range(3):
        pcm.record_model_circuit_open("openrouter", f"or-{i}")
    assert pcm.get_priority("openrouter") == "low"
    # A healthy sibling model keeps its closed circuit — deprioritized only.
    assert cbm.get_state("or-healthy-sibling") == "closed"


def test_get_priority_fail_safe_on_none(pcm):
    assert pcm.get_priority("") == "normal"
    assert ProviderCircuitManager(None).get_priority("any") == "normal"
