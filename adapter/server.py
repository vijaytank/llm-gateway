"""
adapter/server.py — FastAPI Anthropic Inbound Adapter server

Minimal FastAPI service that:
- Sits on port 4001
- Handles Anthropic /v1/messages → OpenAI format → gateway port 4000
- Translates response back to Anthropic format (JSON and SSE streaming,
  branched on the request's own `stream` flag per plan Issue 3)
- Provides health check endpoint

Error responses use Anthropic-style envelopes; internal exception detail is
logged server-side only (never echoed to clients).
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json
import logging
import os

import httpx

from adapter.schemas import AnthropicMessageRequest
from adapter.translation import (
    translate_anthropic_to_openai_request,
    translate_openai_to_anthropic_response,
    translate_stop_reason_from_openai,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global gateway base URL — configurable, never hardcoded (plan DoD).
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost:4000")

# Injectable client factory (unit tests substitute this to stub the gateway;
# production uses a fresh httpx.AsyncClient per call).
def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()

app = FastAPI(
    title="Anthropic Inbound Adapter",
    description="Translates Anthropic Messages API requests to OpenAI format and vice versa",
    version="1.1.0",
)

# Named router so adapter/__init__ (and tests) can reference the routes.
router = app.router


def _anthropic_error(status_code: int, err_type: str, message: str) -> JSONResponse:
    """Anthropic-style error envelope. Internal detail stays in logs only."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "anthropic-adapter", "version": "1.1.0"}


def _forward_headers(request_headers=None) -> dict:
    """Auth headers for gateway calls. The gateway enforces its master key, so
    forwarded requests must carry it — from the client's own Authorization
    header (Anthropic clients send x-api-key; OpenAI-style send Bearer), or
    from GATEWAY_API_KEY env as fallback. A forwarded key that does not match
    GATEWAY_API_KEY (e.g. a client's third-party key) is replaced by the env
    key rather than forwarded and rejected."""
    headers = {}
    env_key = os.environ.get("GATEWAY_API_KEY", "")
    if request_headers is not None:
        auth = request_headers.get("authorization")
        api_key = request_headers.get("x-api-key")
        candidate = None
        if auth:
            candidate = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
        elif api_key:
            candidate = api_key.strip()
        # Forward only if it matches the gateway's expected key.
        if candidate and env_key and candidate == env_key:
            headers["Authorization"] = f"Bearer {candidate}"
    if not headers.get("Authorization") and env_key:
        headers["Authorization"] = f"Bearer {env_key}"
    return headers


async def _stream_gateway_to_anthropic(oai_request: dict, headers: dict):
    """Forward a streaming request to the gateway and yield Anthropic SSE events.

    Owns its httpx client/stream INSIDE the generator: Starlette starts
    consuming after the handler returns, so the context managers must live
    here (httpx.StreamClosed otherwise).
    """
    async with _new_client() as client:
        async with client.stream(
            "POST",
            f"{GATEWAY_BASE_URL}/v1/chat/completions",
            json=oai_request,
            headers=headers,
            timeout=60.0,
        ) as gateway_response:
            # Persistent translation state across the whole stream.
            msg_state = {"message_started": False, "text_started": False}
            block_index = 0
            buffer = ""

            def _sse(event_type: str, payload: dict) -> str:
                return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"

            async for raw_chunk in gateway_response.aiter_text():
                if not raw_chunk:
                    continue
                buffer += raw_chunk
                # Parse complete SSE frames ("data: ...\n") out of the buffer
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        logger.warning("Non-JSON SSE data: %s", data_str[:120])
                        continue

                    choices = chunk.get("choices") or []
                    delta = (choices[0].get("delta") or {}) if choices else {}
                    finish_reason = choices[0].get("finish_reason") if choices else None

                    if not msg_state["message_started"]:
                        yield _sse("message_start", {
                            "type": "message_start",
                            "message": {"role": "assistant", "content": []},
                        })
                        msg_state["message_started"] = True

                    content = delta.get("content")
                    if content:
                        if not msg_state["text_started"]:
                            yield _sse("content_block_start", {
                                "type": "content_block_start",
                                "index": block_index,
                                "content_block": {"type": "text", "text": ""},
                            })
                            msg_state["text_started"] = True
                        yield _sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "text_delta", "text": content},
                        })

                    if finish_reason:
                        stop_reason = translate_stop_reason_from_openai(finish_reason)
                        yield _sse("message_delta", {
                            "type": "message_delta",
                            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                        })
                        yield _sse("message_stop", {"type": "message_stop"})


@app.post("/v1/messages")
async def anthropic_messages(request: AnthropicMessageRequest, raw_request: Request):
    """
    Handle Anthropic /v1/messages POST.

    Translates Anthropic request shape to OpenAI chat/completions format,
    forwards to the gateway, then translates back to Anthropic format.
    Streaming requests (`"stream": true` in the body — what every Anthropic
    SDK client sends) get an SSE response; non-streaming get plain JSON.
    """
    oai_request = translate_anthropic_to_openai_request(request.model_dump())
    headers = _forward_headers(raw_request.headers)

    try:
        if request.stream:
            return StreamingResponse(
                _stream_gateway_to_anthropic(oai_request, headers),
                media_type="text/event-stream",
            )

        async with _new_client() as client:
            response = await client.post(
                f"{GATEWAY_BASE_URL}/v1/chat/completions",
                json=oai_request,
                headers=headers,
                timeout=60.0,
            )
    except Exception as e:
        logger.error("gateway call failed: %s: %s", e.__class__.__name__, e)
        status = 504 if isinstance(e, httpx.TimeoutException) else (
            503 if isinstance(e, httpx.ConnectError) else 500)
        raise HTTPException(status_code=status,
                            detail="upstream gateway failure") from e

    # Non-streaming: translate the OpenAI JSON response back to Anthropic shape.
    anth_response = translate_openai_to_anthropic_response(response.json())
    return anth_response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=4001)
