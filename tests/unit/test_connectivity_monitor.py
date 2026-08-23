"""
tests/unit/test_connectivity_monitor.py — Phase 3 offline detection unit tests.

Covers the plan's test cases:
- UDP fail + >=2 provider connection errors → offline_mode set in Redis
- UDP fail but providers return 429 (rate limit, not connection error) → NOT offline
- UDP ok → NOT offline regardless of provider errors
- offline key auto-expires (TTL) if monitor stops refreshing
"""

import time
from unittest.mock import MagicMock

import pytest

from brain.connectivity_monitor import (
    CONNECTION_ERRORS_KEY,
    ConnectivityMonitor,
    OFFLINE_KEY,
    OFFLINE_REASON_KEY,
)


def make_redis():
    r = MagicMock()
    r.get.return_value = None
    r.zrange.return_value = []
    r.delete.return_value = 0
    return r


def make_monitor(redis_client, **kwargs):
    defaults = dict(
        redis_client=redis_client,
        host="1.1.1.1",
        port=53,
        interval_seconds=1,
        min_provider_failures=2,
        connection_error_window_seconds=120,
        refresh_ttl_seconds=60,
    )
    defaults.update(kwargs)
    return ConnectivityMonitor(**defaults)


def seed_conn_errors(redis_client, providers, age_seconds=0):
    """Seed connection errors for the given providers inside the window."""
    ts = time.time() - age_seconds
    members = {f"{p}:{i}": ts for i, p in enumerate(providers)}
    redis_client.zrange.return_value = list(members.keys())


def test_udp_fail_two_provider_conn_errors_sets_offline():
    r = make_redis()
    seed_conn_errors(r, ["nvidia", "groq"])
    m = make_monitor(r)
    m.count_providers_with_connection_errors()  # exercises prune path
    offline, reason = m.evaluate(udp_ok=False)
    assert offline is True
    assert "connection" in reason.lower()
    m.apply_state(offline, reason)
    r.setex.assert_called_once_with(OFFLINE_KEY, 60, "1")
    r.set.assert_called_once()


def test_udp_fail_but_only_rate_limits_not_offline():
    """429s are recorded with error_type=rate_limit — they never count."""
    r = make_redis()
    seed_conn_errors(r, [])
    m = make_monitor(r)
    # record_provider_error with rate_limit returns False and writes nothing
    assert m.record_provider_error("nvidia", "rate_limit") is False
    r.zadd.assert_not_called()
    offline, reason = m.evaluate(udp_ok=False)
    assert offline is False


def test_auth_errors_never_trigger_offline():
    r = make_redis()
    m = make_monitor(r)
    assert m.record_provider_error("nvidia", "auth_error") is False
    assert m.record_provider_error("groq", "rate_limit") is False
    r.zadd.assert_not_called()


def test_udp_ok_not_offline_despite_provider_errors():
    r = make_redis()
    seed_conn_errors(r, ["nvidia", "groq", "cerebras"])
    m = make_monitor(r)
    offline, reason = m.evaluate(udp_ok=True)
    assert offline is False
    m.apply_state(offline, reason)
    r.delete.assert_called_with(OFFLINE_KEY)


def test_one_provider_failure_not_enough():
    """Single provider connection error → suspect misconfig, stay online."""
    r = make_redis()
    seed_conn_errors(r, ["nvidia"])
    m = make_monitor(r)
    offline, reason = m.evaluate(udp_ok=False)
    assert offline is False
    assert "misconfiguration" in reason


def test_stale_connection_errors_pruned():
    """Errors outside the 120s window are pruned via zremrangebyscore cutoff."""
    r = make_redis()
    seed_conn_errors(r, ["nvidia", "groq"], age_seconds=0)
    m = make_monitor(r)
    count = m.count_providers_with_connection_errors()
    args = r.zremrangebyscore.call_args.args
    assert args[0] == CONNECTION_ERRORS_KEY
    assert args[1] == "-inf"
    assert abs(args[2] - (time.time() - 120)) < 5  # cutoff ≈ now - window
    assert count == 2


def test_offline_key_has_ttl_crash_safety():
    """apply_state must use setex (TTL) so a crashed monitor auto-recovers."""
    r = make_redis()
    seed_conn_errors(r, ["nvidia", "groq"])
    m = make_monitor(r)
    m.apply_state(True, "test")
    # setex with refresh_ttl_seconds=60 → key expires 60s after last refresh
    args = r.setex.call_args
    assert args.args[1] == 60
    assert args.args[2] == "1"


def test_recovery_deletes_offline_key():
    r = make_redis()
    r.get.return_value = b"1"  # currently offline
    r.delete.return_value = 1
    m = make_monitor(r)
    m.apply_state(False, "udp_probe_ok")
    deleted_keys = [c.args[0] for c in r.delete.call_args_list]
    assert OFFLINE_KEY in deleted_keys
    assert OFFLINE_REASON_KEY in deleted_keys


def test_is_offline_static_helper():
    r = make_redis()
    r.get.return_value = b"1"
    assert ConnectivityMonitor.is_offline(r) is True
    r.get.return_value = None
    assert ConnectivityMonitor.is_offline(r) is False
    assert ConnectivityMonitor.is_offline(None) is False


def test_udp_probe_real_localhost_smoke():
    """Sanity: the UDP probe runs against loopback without raising."""
    m = make_monitor(make_redis(), host="127.0.0.1", port=1)
    result = m.udp_probe(timeout=0.5)
    assert isinstance(result, bool)
