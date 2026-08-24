"""Phase 2 unit tests: brain/scorer.py (plan test_scorer.py).

compute_score() matches the Issue 5 formula; edge cases per plan:
0 requests (neutral default), all failures, all successes with low latency,
open circuit -> -1, half-open -> 0.
"""

import pytest

from brain import scorer
from brain.circuit_breaker import CircuitBreakerManager


def test_no_data_returns_neutral_default(fake_redis):
    """0 requests → neutral score (success_rate 0.5, latency 0.5, headroom 1.0)."""
    s = scorer.compute_score("m1")
    expected = 0.5 * 0.40 + 0.5 * 0.35 + 1.0 * 0.25
    assert abs(s - expected) < 1e-6


def test_score_within_valid_range(fake_redis):
    for latency in (0, 5000, 20000):
        s = scorer.compute_score(f"m-range-{latency}", latency_ms=latency)
        assert -1.0 <= s <= 1.0


def test_all_success_low_latency_scores_high(fake_redis):
    fake_redis.rpush("gateway:model:fast-good:outcome_window", *(["1"] * 20))
    fake_redis.hset("gateway:model:fast-good:limits", mapping={"rpm": 100})
    s = scorer.compute_score("fast-good", latency_ms=0)
    assert s == pytest.approx(1.0, abs=0.01)


def test_critical_latency_drives_latency_term_to_zero(fake_redis):
    slow = scorer.compute_score("slow-model", latency_ms=20000)
    fast = scorer.compute_score("fast-model", latency_ms=200)
    assert fast > slow


def test_circuit_open_excluded(fake_redis):
    cb = CircuitBreakerManager(fake_redis)
    cb._set_state("broken", "open", ttl=600)
    assert scorer.compute_score("broken") == -1.0


def test_half_open_last_resort(fake_redis):
    cb = CircuitBreakerManager(fake_redis)
    cb._set_state("warming", "half_open", ttl=600)
    assert scorer.compute_score("warming") == 0.0


def test_formula_matches_issue5_documented_vectors(fake_redis):
    """AC: score computation matches formula output for documented vectors."""
    from brain.config import (
        SCORE_WEIGHT_SUCCESS_RATE, SCORE_WEIGHT_LATENCY, SCORE_WEIGHT_QUOTA_HEADROOM)

    def formula(success_rate, latency_ms, used, limit):
        norm = min(latency_ms, 20000) / 20000
        headroom = max(0.0, min(1.0, 1.0 - used / limit))
        return (success_rate * SCORE_WEIGHT_SUCCESS_RATE
                + (1 - norm) * SCORE_WEIGHT_LATENCY
                + headroom * SCORE_WEIGHT_QUOTA_HEADROOM)

    vectors = [
        (0.95, 800, 10, 100),   # healthy workhorse
        (0.70, 6000, 50, 100),  # degraded
        (0.40, 15000, 90, 100), # near-exhausted quota
        (1.00, 0, 0, 100),      # pristine
        (0.55, 12000, 20, 80),  # middling
    ]
    for i, (sr, lat, used, limit) in enumerate(vectors):
        # New contract: rolling outcome window (last N outcomes; compute_score
        # defaults to window_size=50) + live RPM counter vs seeded limit.
        n = 50
        outcomes = ["1"] * round(sr * n) + ["0"] * (n - round(sr * n))
        fake_redis.rpush(f"gateway:model:v{i}:outcome_window", *outcomes)
        if used:
            fake_redis.set(f"gateway:model:v{i}:quota:rpm", used)
        fake_redis.hset(f"gateway:model:v{i}:limits", mapping={"rpm": limit})
        s = scorer.compute_score(f"v{i}", latency_ms=lat)
        assert s == pytest.approx(formula(sr, lat, used, limit), abs=0.02), f"vector {i}"


def test_fresh_failure_not_masked_by_history(fake_redis):
    """Regression (review F-M4): recent failures must move the score even when
    long history is all-success (rolling window, not cumulative counters)."""
    r = fake_redis
    r.rpush("gateway:model:flappy:outcome_window", *(["1"] * 50))
    good = scorer.compute_score("flappy", latency_ms=200)
    r.rpush("gateway:model:flappy:outcome_window", *(["0"] * 20))
    degraded = scorer.compute_score("flappy", latency_ms=200)
    assert degraded < good - 0.05


def test_quota_exhaustion_lowers_headroom_term(fake_redis):
    """Regression (review F-M5): live RPM counter feeds quota headroom."""
    r = fake_redis
    r.rpush("gateway:model:busy:outcome_window", *(["1"] * 30))
    r.hset("gateway:model:busy:limits", mapping={"rpm": 10})
    fresh = scorer.compute_score("busy", latency_ms=200)
    r.set("gateway:model:busy:quota:rpm", 10)  # fully exhausted this minute
    exhausted = scorer.compute_score("busy", latency_ms=200)
    assert exhausted < fresh
