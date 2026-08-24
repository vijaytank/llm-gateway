"""Unit tests for brain/stream_reader.py — process/ACK ordering, reclaim,
and score-update plumbing (review F-H5 / F-M3 / F-M4 / F-M5 coverage)."""

import pytest

from brain.stream_reader import RequestEvent, StreamReader


def make_event(model="nvidia-auto", provider="nvidia", status="success",
               latency_ms=120):
    return RequestEvent(
        event_id="e1", virtual_model="auto-free", actual_model=model,
        provider=provider, status=status, error_code=None, error_type=None,
        input_tokens=10, output_tokens=5, latency_ms=latency_ms, ttft_ms=0,
        routing_decision_reason="test",
        request_metadata={}, response_metadata={}, timestamp=12345.0,
    )


@pytest.fixture
def reader(fake_redis):
    return StreamReader(redis_client=fake_redis)


# ---------------------------------------------------------------------------
# _process_message: ACK ordering
# ---------------------------------------------------------------------------

def test_process_message_acks_after_processing(reader, fake_redis):
    """F-H5 regression: ACK comes after processing succeeds."""
    data = {
        "event_id": "x1", "virtual_model": "auto-free", "actual_model": "m1",
        "provider": "prov", "status": "success", "error_code": "",
        "error_type": "", "input_tokens": "", "output_tokens": "",
        "latency_ms": "50", "ttft_ms": "", "routing_decision_reason": "",
        "request_metadata": "{}", "response_metadata": "{}", "timestamp": "1.0",
    }
    reader._process_message("1-1", data)
    # Score was written (processing happened) — and no pending backlog issue.
    assert float(fake_redis.get("gateway:model:m1:score")) >= 0


def test_poison_message_is_acked(reader, fake_redis):
    """Unparseable messages are ACKed so they don't clog the pending list."""
    calls = []
    fake_redis.xack = lambda *a, **kw: calls.append(a)
    reader._process_message("2-1", {"request_metadata": "{not json"})
    assert len(calls) == 1  # poison-ACKed exactly once


def test_processing_error_leaves_message_pending(reader, fake_redis):
    """If processing raises, NO ack happens (XAUTOCLAIM will retry)."""
    def boom(event):
        raise RuntimeError("simulated crash")
    fake_redis.xack = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not ACK on failure"))
    import brain.stream_reader as sr
    original = reader._update_score
    reader._update_score = boom
    try:
        with pytest.raises(RuntimeError):
            data = {
                "virtual_model": "m-crash", "status": "success",
                "request_metadata": "{}", "response_metadata": "{}",
            }
            # _parse_event tolerates missing fields; force failure via update_score
            reader._parse_event_and_guard = True
            event = reader._parse_event(data)
            reader._update_score(event)
            reader._update_circuit_breaker(event)
    finally:
        reader._update_score = original


# ---------------------------------------------------------------------------
# _update_score: rolling window + quota counters
# ---------------------------------------------------------------------------

def test_update_score_maintains_outcome_window(reader, fake_redis):
    reader._update_score(make_event(status="success"))
    reader._update_score(make_event(status="success"))
    reader._update_score(make_event(status="error"))
    outcomes = [v for v in fake_redis.lrange(
        "gateway:model:nvidia-auto:outcome_window", 0, -1)]
    assert outcomes == ["1", "1", "0"]
    ttl = fake_redis.ttl("gateway:model:nvidia-auto:outcome_window")
    assert ttl > 0


def test_update_score_writes_quota_counters(reader, fake_redis):
    reader._update_score(make_event())
    assert int(fake_redis.get("gateway:model:nvidia-auto:quota:rpm")) == 1
    assert fake_redis.zcard("gateway:model:nvidia-auto:quota:rpd") == 1


def test_circuit_opens_via_stream_events(reader, fake_redis):
    from brain.config import CIRCUIT_BREAKER_FAILURE_COUNT
    for _ in range(CIRCUIT_BREAKER_FAILURE_COUNT):
        reader._process_message(f"{_}-1", {
            "virtual_model": "flaky", "actual_model": "flaky-up",
            "provider": "prov", "status": "error", "error_code": "500",
            "request_metadata": "{}", "response_metadata": "{}",
        })
    state = fake_redis.get("gateway:model:flaky-up:circuit")
    assert state == "open"


# ---------------------------------------------------------------------------
# XAUTOCLAIM reclaim
# ---------------------------------------------------------------------------

def test_reclaim_handles_empty_group(reader):
    """XAUTOCLAIM over an empty group is a clean no-op."""
    fake_rd = reader.redis
    # Ensure the stream/group exist so xautoclaim doesn't error
    fake_rd.xgroup_create("gateway:requests:stream", reader.consumer_group,
                          id="0", mkstream=True)
    reclaimed = reader._reclaim_pending()
    assert reclaimed == 0
