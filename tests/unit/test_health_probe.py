"""Phase 1 unit tests: gateway/health_startup.py probe classification
(plan test_health_probe.py).

200 -> healthy, 429 -> rate_limited, 400 with content_filter body -> healthy,
401 -> unauthorized, 503 -> unhealthy.
"""

import pytest

from brain.health_scheduler import HealthScheduler


@pytest.fixture
def scheduler(fake_redis):
    return HealthScheduler(redis_client=fake_redis)


def test_200_is_healthy(scheduler):
    cls, ok = scheduler._classify_probe_response(200, {"choices": []}, None)
    assert cls == "healthy" and ok is True


def test_429_is_rate_limited_not_unhealthy(scheduler):
    cls, ok = scheduler._classify_probe_response(429, None, None)
    assert cls == "rate_limited"
    # rate-limited means the model is UP — not an outage signal


def test_400_content_filter_is_healthy(scheduler):
    body = {"error": {"type": "invalid_request_error",
                      "message": "blocked by content_filter policy"}}
    cls, ok = scheduler._classify_probe_response(400, body, None)
    assert cls == "content_filter" and ok is True


def test_400_invalid_request_is_misconfigured(scheduler):
    body = {"error": {"type": "invalid_request_error", "message": "bad param"}}
    cls, ok = scheduler._classify_probe_response(400, body, None)
    assert ok is False


def test_401_is_unauthorized(scheduler):
    cls, ok = scheduler._classify_probe_response(401, None, None)
    assert cls == "unauthorized" and ok is False


def test_503_is_unhealthy(scheduler):
    cls, ok = scheduler._classify_probe_response(503, None, None)
    assert cls == "unhealthy" and ok is False


def test_structured_probe_payload_not_ping():
    """Issue 4 fix: probe payload is the structured 'Reply OK' message."""
    import inspect
    import brain.health_scheduler as hsm
    src = inspect.getsource(hsm.HealthScheduler._probe_model)
    assert "Reply with the single word OK." in src
    assert '"ping"' not in src and "'ping'" not in src


def test_probe_result_writes_redis_status(scheduler, fake_redis):
    scheduler._on_probe_result("nvidia-auto", "healthy", healthy=True)
    status = fake_redis.get("gateway:model:nvidia-auto:status")
    assert status == "healthy"

    scheduler._on_probe_result("groq-auto-free", "unhealthy", healthy=False)
    status = fake_redis.get("gateway:model:groq-auto-free:status")
    assert status == "unhealthy"
