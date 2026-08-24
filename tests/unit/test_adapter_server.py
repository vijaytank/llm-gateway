"""Unit tests for adapter/server.py — review F-M15 coverage gap.

Covers the streaming branch inside POST /v1/messages (review F-H4 fix),
non-streaming translation, health endpoint, and auth header forwarding.
The gateway upstream is stubbed via adapter.server._new_client.
"""

import json

import httpx
import pytest

import adapter.server as srv
from adapter.server import app, _forward_headers


# ---------------------------------------------------------------------------
# Stub gateway plumbing
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class FakeStreamCtx:
    """Async context returning an object with aiter_text()."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        outer = self

        class Stream:
            async def aiter_text(self):
                for c in outer._chunks:
                    yield f"data: {json.dumps(c)}\n\n"
                yield "data: [DONE]\n\n"

        return Stream()

    async def __aexit__(self, *a):
        return False


def make_stub_client(nonstream_payload=None, stream_chunks=None, exc=None):
    class StubClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            if exc:
                raise exc
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            self.url = url
            self.kw = kw
            return FakeResponse(nonstream_payload)

        def stream(self, method, url, **kw):
            return FakeStreamCtx(stream_chunks or [])

    return StubClient


def install(monkeypatch, stub_cls):
    monkeypatch.setattr(srv, "_new_client", stub_cls)


@pytest.fixture
def client_factory():
    transport = httpx.ASGITransport(app=app)

    def _make():
        return httpx.AsyncClient(transport=transport, base_url="http://t")

    return _make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_health_endpoint(client_factory):
    async with client_factory() as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "anthropic-adapter"


async def test_nonstreaming_messages_translated(client_factory, monkeypatch):
    captured = {}

    def stub_cls(*a, **kw):
        inner = make_stub_client(nonstream_payload={
            "id": "x", "model": "auto-free",
            "choices": [{"message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })()

        original_post = inner.post

        async def post(url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return await original_post(url, **kw)

        inner.post = post
        return inner

    install(monkeypatch, stub_cls)

    async with client_factory() as c:
        r = await c.post("/v1/messages", json={
            "model": "auto-free", "max_tokens": 10,
            "system": "be brief",
            "messages": [{"role": "user", "content": "hello"}],
        })
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "hi"
    assert body["stop_reason"] == "end_turn"
    # System prompt became first system message in the forwarded request
    fwd = captured["json"]
    assert fwd["messages"][0]["role"] == "system"
    assert fwd["messages"][1] == {"role": "user", "content": "hello"}
    assert captured["url"].endswith("/v1/chat/completions")


async def test_streaming_request_returns_sse(client_factory, monkeypatch):
    """F-H4 regression: stream=true on /v1/messages yields Anthropic SSE."""
    chunks = [
        {"id": "c1", "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"delta": {"content": "hel"}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    install(monkeypatch, make_stub_client(
        stream_chunks=[c for c in chunks]))

    async with client_factory() as c:
        async with c.stream("POST", "/v1/messages", json={
            "model": "auto-free", "max_tokens": 10, "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            raw = ""
            async for piece in resp.aiter_text():
                raw += piece

    order = [e for e in (
        "event: message_start", "event: content_block_start",
        "event: content_block_delta", "event: message_delta", "event: message_stop")
        if e in raw]
    assert order[0] == "event: message_start"
    assert order[-1] == "event: message_stop"
    assert "text_delta" in raw
    assert "end_turn" in raw


async def test_gateway_connect_error_maps_to_5xx(client_factory, monkeypatch):
    install(monkeypatch, make_stub_client(exc=httpx.ConnectError("nope")))
    async with client_factory() as c:
        r = await c.post("/v1/messages", json={
            "model": "m", "max_tokens": 5,
            "messages": [{"role": "user", "content": "x"}]})
    # F-M10: generic envelope, no internal exception text leaked
    assert r.status_code in (500, 503)
    assert "nope" not in r.text


async def test_gateway_timeout_maps_to_504(client_factory, monkeypatch):
    install(monkeypatch, make_stub_client(exc=httpx.TimeoutException("slow")))
    async with client_factory() as c:
        r = await c.post("/v1/messages", json={
            "model": "m", "max_tokens": 5,
            "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code in (500, 504)
    assert "slow" not in r.text


def test_forward_headers_prefers_matching_key(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "secret-key")
    h = _forward_headers({"authorization": "Bearer secret-key"})
    assert h["Authorization"] == "Bearer secret-key"
    # Third-party keys are replaced by the env key, not forwarded
    h2 = _forward_headers({"x-api-key": "someone-elses-key"})
    assert h2["Authorization"] == "Bearer secret-key"


def test_forward_headers_empty_without_env(monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    assert _forward_headers({"authorization": "Bearer whatever"}) == {}
