"""Phase 2 unit tests: brain/health_scheduler.py (plan test_health_scheduler.py).

- Model used recently with no errors → probe interval is the base 2h (not accelerated)
- Model not probed in 2h → needs probe
- Model with consecutive errors → backoff doubles
- Jitter applied: intervals vary by ±10%
"""

import time

import pytest

from brain.health_scheduler import HealthScheduler
from brain.config import HEALTH_CHECK_BASE_INTERVAL_S


@pytest.fixture
def scheduler(fake_redis):
    return HealthScheduler(redis_client=fake_redis)


def _used_recently(scheduler, model):
    """Model used 30 min ago with no errors: base interval applies."""
    scheduler._last_probe[model] = time.time() - 1800
    scheduler._consecutive_errors[model] = 0
    scheduler._last_success[model] = time.time() - 1800


def test_recently_used_no_errors_not_due(scheduler):
    model = "m-recent"
    _used_recently(scheduler, model)
    assert scheduler._needs_probe(model, time.time()) is False


def test_not_probed_for_2h_is_due(scheduler):
    model = "m-stale"
    scheduler._last_probe[model] = time.time() - HEALTH_CHECK_BASE_INTERVAL_S * 1.2
    scheduler._consecutive_errors[model] = 0
    assert scheduler._needs_probe(model, time.time()) is True


def test_never_probed_is_due(scheduler):
    assert scheduler._needs_probe("brand-new", time.time()) is True


def test_consecutive_errors_double_interval(scheduler):
    """Model with errors: next check interval grows with the backoff multiplier."""
    base = scheduler._get_probe_interval("clean")   # no errors
    scheduler._consecutive_errors["failing"] = 3
    scheduler._last_success["failing"] = time.time() - 3600
    failing = scheduler._get_probe_interval("failing")
    # 3 consecutive errors → multiplier^(3-1) = 4x nominal (before jitter clamp)
    assert failing > base * 2


def test_jitter_varies_within_10_percent(scheduler):
    scheduler._consecutive_errors.clear()
    intervals = [scheduler._get_probe_interval("jitter-model") for _ in range(20)]
    lo, hi = min(intervals), max(intervals)
    spread = (hi - lo) / max(lo, 1e-9)
    assert 0 < spread < 0.25   # some jitter, bounded (~±10% of nominal)


def test_probe_targets_scan_redis(scheduler, fake_redis):
    fake_redis.setex("gateway:model:m1:status", 7200, "healthy")
    targets = scheduler._determine_probe_targets(time.time())
    names = {t["model_name"] for t in targets}
    assert "m1" in names
