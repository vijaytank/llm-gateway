"""
test_basic_routing_and_logging.py — Plan Phase 1: routing + metadata logging.

Covers (plan test_basic_routing / test_metadata_logging / test_openai_contract):
  - A request to the virtual model routes through a mock deployment and returns
    valid OpenAI ChatCompletion JSON
  - Streaming returns valid SSE chunks
  - Each successful request writes a request_logs row with non-null
    actual_model, latency_ms, status — via Postgres read-back
  - Each success publishes an event to the gateway:requests:stream Redis stream
"""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (  # noqa: E402
    GATEWAY_URL, gateway_chat, http_json, pg_query, redis_cmd, wait_until,
)


def test_request_routes_to_mock_primary():
    """Healthy chain: primary mock answers; response is valid ChatCompletion."""
    status, body = gateway_chat()
    assert status == 200, body
    data = json.loads(body)
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"]
    assert "Mock reply from" in data["choices"][0]["message"]["content"]
    assert data["usage"]["total_tokens"] > 0


def test_streaming_returns_valid_sse_chunks():
    payload = {
        "model": "auto-free",
        "messages": [{"role": "user", "content": "stream test"}],
        "max_tokens": 20,
        "stream": True,
    }
    req = urllib.request.Request(
        f"{GATEWAY_URL}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        assert resp.status == 200
        content_type = resp.headers.get("Content-Type", "")
        raw = resp.read().decode()

    assert "text/event-stream" in content_type
    events = [line for line in raw.splitlines() if line.startswith("data: ")]
    assert len(events) >= 3, f"expected >=3 SSE chunks, got {len(events)}"
    # First chunk carries role delta; final data is [DONE]
    first = json.loads(events[0][6:])
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"].get("role") == "assistant"
    done = json.loads(events[-2][6:]) if events[-1] == "data: [DONE]" else None
    if done:
        assert done["choices"][0]["finish_reason"] == "stop"
    assert events[-1] == "data: [DONE]"


def test_success_writes_postgres_metadata_log():
    """AC: after N requests, request_logs has N rows with non-null fields."""
    n = 5
    for _ in range(n):
        status, _ = gateway_chat()
        assert status == 200

    def _count():
        val = pg_query(
            "SELECT COUNT(*) FROM request_logs WHERE status='success'", fetch="one")
        return int(val) >= n

    wait_until(_count, timeout=30, desc="request_logs rows")

    rows = pg_query(
        "SELECT actual_model, provider, latency_ms, status, virtual_model "
        "FROM request_logs WHERE status='success' LIMIT 10")
    assert len(rows) >= n
    for actual_model, provider, latency_ms, status_col, virtual_model in rows:
        assert actual_model, "actual_model null"
        assert provider, "provider null"
        assert latency_ms is not None, "latency_ms null"
        assert status_col == "success"


def test_success_publishes_redis_stream_event():
    before = _stream_len() or 0
    status, _ = gateway_chat()
    assert status == 200

    def _grew():
        return (_stream_len() or 0) > before
    wait_until(_grew, timeout=30, desc="Redis stream growth")


def _stream_len():
    out = redis_cmd("xlen", "gateway:requests:stream")
    try:
        return int(out)
    except (TypeError, ValueError):
        return None
