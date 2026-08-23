"""
tests/integration/mock_provider.py — Scriptable OpenAI-compatible mock provider

A tiny FastAPI app that speaks the OpenAI Chat Completions protocol so
LiteLLM can route to it as an `openai/`-compatible upstream. Test behavior is
driven ENTIRELY at runtime through Redis keys (no code changes, no restarts):

  mock:status:{model}          = 200 | 429 | 500 | 503 | timeout | slow:<ms>   (absent = 200)
  mock:latency_ms:{model}      = integer added latency before responding
  mock:content                 = override response text (default "Mock reply from <model>")
  mock:request_count:{model}   = auto-incremented on every request (read by tests)

This file lives under tests/integration and runs ONLY inside a container
started from the compose `testing` profile — it is never imported by, or
shipped with, production code.
"""

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import redis as redis_lib

app = FastAPI(title="Mock OpenAI-compatible provider")

REDIS_URL = os.environ.get("MOCK_REDIS_URL", os.environ.get("REDIS_URL", "redis://redis:6379/0"))
PORT = int(os.environ.get("MOCK_PROVIDER_PORT", "5000"))


def _r() -> "redis_lib.Redis":
    return redis_lib.from_url(REDIS_URL, decode_responses=True)


def _models() -> list[str]:
    """Served models come from env (set by compose), never hardcoded."""
    raw = os.environ.get("MOCK_MODELS", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock-provider"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in _models()],
    }


def _error_body(status: int) -> dict[str, Any]:
    if status == 429:
        return {"error": {"message": "mock rate limit exceeded", "type": "rate_limit_error", "code": 429}}
    if status in (500, 503):
        return {"error": {"message": f"mock server error {status}", "type": "server_error", "code": status}}
    return {"error": {"message": f"mock error {status}", "type": "api_error", "code": status}}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "unknown")
    r = _r()
    r.incr(f"mock:request_count:{model}")

    status_raw = r.get(f"mock:status:{model}")
    latency_ms = int(r.get(f"mock:latency_ms:{model}") or 0)
    if latency_ms:
        await asyncio.sleep(latency_ms / 1000.0)

    if status_raw == "timeout":
        await asyncio.sleep(120)  # far beyond any client timeout
        return JSONResponse(_error_body(500), status_code=500)

    if status_raw and status_raw.startswith("slow:"):
        delay = float(status_raw.split(":", 1)[1]) / 1000.0
        await asyncio.sleep(delay)

    status = int(status_raw) if status_raw and status_raw.isdigit() else 200
    if status != 200:
        return JSONResponse(_error_body(status), status_code=status)

    content = r.get("mock:content") or f"Mock reply from {model}"
    created = int(time.time())
    completion_id = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"

    if body.get("stream"):
        async def sse():
            # role-first chunk then content chunk, mirroring real OpenAI SSE
            first = {
                "id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            second = {
                "id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
            }
            final = {
                "id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
            }
            for chunk in (first, second, final):
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
