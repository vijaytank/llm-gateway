"""
tests/unit/test_error_accounting.py — Automatic connectivity error accounting.

Covers the full pipeline: failure event → classify → record → offline decision.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from brain.connectivity_monitor import (
    CONNECTION_ERRORS_KEY,
    ConnectivityMonitor,
    classify_error,
    is_connection_failure,
)
from brain.stream_reader import RequestEvent, StreamReader


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------

def test_classify_rate_limit():
    assert classify_error(error_code="429") == "rate_limit"
    assert classify_error(error_type="RateLimitError") == "rate_limit"
    assert classify_error(error_type="rate_limit") == "rate_limit"


def test_classify_auth():
    assert classify_error(error_code="401") == "auth_error"
    assert classify_error(error_code=403) == "auth_error"
    assert classify_error(error_type="AuthenticationError") == "auth_error"


def test_classify_connection_failures():
    assert classify_error(error_type="APIConnectionError") == "connection_error"
    assert classify_error(error_type="connection refused") == "connection_error"
    assert classify_error(error_type="TimeoutError") == "timeout"
    assert classify_error(error_type="dns resolution failed") == "connection_error"


def test_classify_server_errors():
    for code in ("500", "502", "503", "504"):
        assert classify_error(error_code=code) == "server_error"


def test_classify_unknown_is_safe():
    assert classify_error(error_code=None, error_type=None) == "unknown"
    assert classify_error(error_code="weird", error_type="??") == "unknown"
    # Never raises on garbage
    assert classify_error(error_code=object(), error_type=12345) in (
        "unknown", "server_error", "connection_error", "timeout")


def test_is_connection_failure_matches_issue_10_rules():
    # Connection-class failures count
    assert is_connection_failure(error_type="connection_error")
    assert is_connection_failure(error_type="timeout")
    # Auth and rate limit NEVER count (Issue 10)
    assert not is_connection_failure(error_type="auth_error")
    assert not is_connection_failure(error_type="rate_limit")
    # Plain 5xx is a server error, not a connection error — the provider was
    # reached, so it doesn't indicate internet loss.
    assert not is_connection_failure(error_type="server_error")


# ---------------------------------------------------------------------------
# Stream reader accounting
# ---------------------------------------------------------------------------

def make_reader(redis_client):
    return StreamReader(
        redis_client=redis_client,
        connection_error_window_seconds=120,
    )


def make_event(provider="nvidia", status="error", error_type="connection_error",
               error_code=None, actual_model="nvidia-auto", timestamp=None):
    return RequestEvent(
        event_id="evt-1",
        virtual_model="auto-free",
        actual_model=actual_model,
        provider=provider,
        status=status,
        error_code=error_code,
        error_type=error_type,
        input_tokens=None, output_tokens=None,
        latency_ms=None, ttft_ms=None,
        routing_decision_reason=None,
        request_metadata={}, response_metadata={},
        timestamp=timestamp if timestamp is not None else time.time(),
    )


def test_connection_error_recorded():
    r = MagicMock()
    r.zadd.return_value = 1
    reader = make_reader(r)
    reader._record_connectivity_error(make_event())
    r.zadd.assert_called_once()
    key, mapping = r.zadd.call_args.args
    assert key == CONNECTION_ERRORS_KEY
    assert list(mapping.keys())[0].startswith("nvidia:")


def test_rate_limit_not_recorded():
    r = MagicMock()
    reader = make_reader(r)
    reader._record_connectivity_error(make_event(error_type="rate_limit"))
    r.zadd.assert_not_called()


def test_auth_error_not_recorded():
    r = MagicMock()
    reader = make_reader(r)
    reader._record_connectivity_error(make_event(error_type="auth_error"))
    r.zadd.assert_not_called()


def test_success_not_recorded():
    r = MagicMock()
    reader = make_reader(r)
    reader._record_connectivity_error(make_event(status="success", error_type=None))
    r.zadd.assert_not_called()


def test_local_provider_not_recorded():
    r = MagicMock()
    reader = make_reader(r)
    for provider, model in (("ollama", "local-llama3-8b"), ("local", "local-auto"), ("", "nvidia-auto")):
        reader._record_connectivity_error(make_event(provider=provider, actual_model=model))
    r.zadd.assert_not_called()


def test_unknown_error_type_auto_classified():
    """error_type='unknown' + error_code=503 → server_error → NOT counted as
    connection failure (provider was reached)."""
    r = MagicMock()
    reader = make_reader(r)
    reader._record_connectivity_error(make_event(error_type="unknown", error_code="503"))
    r.zadd.assert_not_called()


def test_timeout_auto_classified_and_recorded():
    r = MagicMock()
    reader = make_reader(r)
    reader._record_connectivity_error(make_event(error_type="TimeoutError"))
    r.zadd.assert_called_once()


def test_accounting_never_raises():
    """A broken Redis must not break stream processing."""
    r = MagicMock()
    reader = make_reader(r)
    r.zadd.side_effect = ConnectionError("redis down")
    # Should not raise
    reader._record_connectivity_error(make_event())


# ---------------------------------------------------------------------------
# End-to-end: two providers failing → offline decision
# ---------------------------------------------------------------------------

def test_two_providers_connection_errors_trigger_offline():
    """The full automatic path: stream events → zset → evaluate → offline."""
    now = time.time()
    r = MagicMock()
    reader = make_reader(r)
    # Simulate the two XADDs the reader performs for nvidia and groq failures.
    for provider in ("nvidia", "groq"):
        reader._record_connectivity_error(
            make_event(provider=provider, error_type="APIConnectionError")
        )
    # Read back what was written and evaluate.
    members = []
    for call in r.zadd.call_args_list:
        members.extend(call.args[1].keys())
    r.zrange.return_value = members
    monitor = reader.connectivity_monitor
    offline, reason = monitor.evaluate(udp_ok=False)
    assert offline is True
    assert "2 cloud providers" in reason


def test_mixed_failures_stay_online():
    """One connection failure + one rate limit → only 1 provider counts → online."""
    r = MagicMock()
    reader = make_reader(r)
    reader._record_connectivity_error(make_event(provider="nvidia", error_type="APIConnectionError"))
    reader._record_connectivity_error(make_event(provider="groq", error_type="rate_limit"))
    # Only nvidia's zadd happened
    assert r.zadd.call_count == 1
    members = r.zadd.call_args.args[1].keys()
    r.zrange.return_value = list(members)
    offline, _ = reader.connectivity_monitor.evaluate(udp_ok=False)
    assert offline is False
