"""Phase 1 unit tests: gateway/callbacks.py (plan test_callbacks.py).

Success event writes correct fields; failure event includes error_code.
Uses mocked Redis + Postgres connections (no live services).
"""

from unittest.mock import MagicMock

import pytest

import gateway.callbacks as cb_mod
from gateway.callbacks import CustomLogger, RequestEvent


@pytest.fixture
def logger(fake_redis):
    log = CustomLogger(redis_client=fake_redis, postgres_dsn="postgresql://mock")
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    log._pg_conn = mock_conn
    return log


def test_request_event_has_id():
    ev = RequestEvent(
        virtual_model="auto-free", actual_model="nvidia-auto", provider="nvidia",
        status="success", error_code=None, error_type=None,
        input_tokens=10, output_tokens=5, latency_ms=120, ttft_ms=40,
        routing_decision_reason="primary",
        request_metadata={}, response_metadata={},
    )
    assert ev.event_id


def test_success_event_stream_fields(logger, fake_redis):
    logger.log_success_event(
        virtual_model="auto-free", actual_model="nvidia-auto", provider="nvidia",
        input_tokens=10, output_tokens=5, latency_ms=120, ttft_ms=40,
        request_metadata={"client": "test"}, response_metadata={},
    )
    raw = fake_redis.xrange("gateway:requests:stream", count=10)
    assert len(raw) == 1
    fields = {k: v for k, v in raw[0][1].items()}
    assert fields["actual_model"] == "nvidia-auto"
    assert fields["provider"] == "nvidia"
    assert fields["status"] == "success"
    assert fields["latency_ms"] == "120"
    assert fields["input_tokens"] == "10"


def test_failure_event_includes_error_code(logger, fake_redis):
    logger.log_failure_event(
        virtual_model="auto-free", actual_model="nvidia-auto", provider="nvidia",
        error_code="429", error_type="rate_limit",
        latency_ms=50,
        request_metadata={}, response_metadata={},
    )
    raw = fake_redis.xrange("gateway:requests:stream", count=10)
    assert len(raw) == 1
    fields = dict(raw[0][1])
    assert fields["error_code"] == "429"
    assert fields["error_type"] == "rate_limit"
    assert fields["status"] == "error"


def test_callback_does_no_routing_logic(logger, fake_redis):
    """Callbacks must NOT touch routing state keys — that's the brain's job."""
    logger.log_success_event(
        virtual_model="auto-free", actual_model="nvidia-auto", provider="nvidia",
        input_tokens=1, output_tokens=1, latency_ms=1, ttft_ms=1,
        request_metadata={}, response_metadata={},
    )
    for pattern in ("gateway:model:*:score", "gateway:model:*:circuit"):
        assert fake_redis.keys(pattern) == []
