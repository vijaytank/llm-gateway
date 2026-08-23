"""
adapter/server.py — FastAPI Anthropic Inbound Adapter server

Minimal FastAPI service that:
- Sits on port 4001
- Handles Anthropic /v1/messages → OpenAI format → gateway port 4000
- Translates response back to Anthropic format
- Supports streaming translation
- Provides health check endpoint
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import logging
import os

import httpx

from adapter.schemas import AnthropicMessageRequest, AnthropicMessageResponse
from adapter.translation import (
    translate_anthropic_to_openai_request,
    translate_openai_to_anthropic_response,
    translate_anthropic_stream_to_openai,
    translate_stop_reason_from_openai,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global gateway base URL — configurable, never hardcoded (plan DoD).
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost:4000")

app = FastAPI(
    title="Anthropic Inbound Adapter",
    description="Translates Anthropic Messages API requests to OpenAI format and vice versa",
    version="1.0.0",
)

# Named router so adapter/__init__ (and tests) can reference the routes
router = app.router

# Add CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "anthropic-adapter", "version": "1.0.0"}



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


@app.post("/v1/messages")
async def anthropic_messages(
    request: AnthropicMessageRequest,
    raw_request: Request,
):
    """
    Handle Anthropic /v1/messages POST.

    Translates Anthropic request shape to OpenAI chat/completions format,
    forwards to gateway, then translates response back to Anthropic format.
    """
    try:
        # Step 1: Translate Anthropic request to OpenAI format
        oai_request = translate_anthropic_to_openai_request(request.dict())

        # Step 2: Forward to gateway using httpx (with auth headers)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GATEWAY_BASE_URL}/v1/chat/completions",
                json=oai_request,
                headers=_forward_headers(raw_request.headers),
                timeout=60.0,
            )
        
        # Step 3: Translate OpenAI response back to Anthropic format
        anth_response = translate_openai_to_anthropic_response(response.json())
        
        # Step 4: Return Anthropic-formatted response
        return anth_response
        
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Gateway request timed out")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Cannot connect to gateway")
    except Exception as e:
        logger.error(f"Error translating Anthropic request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def stream_anthropic_to_openai(
    anthropic_events: list,
) -> StreamingResponse:
    """
    Stream Anthropic events translated to OpenAI format.
    
    This enables streaming from Anthropic SDK clients through the gateway.
    """
    async def event_generator():
        oai_events = translate_anthropic_stream_to_openai(anthropic_events)
        for event in oai_events:
            yield f"data: {json.dumps(event)}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )


@app.post("/v1/messages/stream")
async def anthropic_messages_stream(
    request: AnthropicMessageRequest,
    raw_request: Request,
):
    """
    Handle Anthropic /v1/messages/stream POST with streaming.
    
    Translates Anthropic streaming events to OpenAI format and streams back.
    """
    try:
        # Step 1: Translate Anthropic request to OpenAI format
        oai_request = translate_anthropic_to_openai_request(request.dict())
        
        # Step 2: Forward to gateway
        async def event_stream():
            # Own the httpx client/stream INSIDE the generator: Starlette only
            # starts consuming this iterator after the handler returns, so the
            # `async with` must not live in the handler body (the connection
            # would already be closed -> httpx.StreamClosed on first read).
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{GATEWAY_BASE_URL}/v1/chat/completions",
                    json=oai_request,
                    headers=_forward_headers(raw_request.headers),
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
                        # Parse complete SSE frames ("data: ...\n\n") out of the buffer
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
                                logger.warning(f"Non-JSON SSE data: {data_str[:120]}")
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

        return StreamingResponse(
                    event_stream(),
                    media_type="text/event-stream",
                )
        
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Gateway request timed out")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Cannot connect to gateway")
    except Exception as e:
        logger.error(f"Error in streaming: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """
    Pass-through endpoint for OpenAI-format requests.
    
    Accepts OpenAI chat completions requests and forwards them to the
    main gateway on port 4000. This allows the adapter to also serve
    as a pass-through for direct OpenAI SDK calls.
    """
    body = await request.json()
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GATEWAY_BASE_URL}/v1/chat/completions",
                json=body,
                headers=_forward_headers(request.headers),
                timeout=60.0,
            )
        
        return response.json()
    
    except Exception as e:
        logger.error(f"Error forwarding OpenAI request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=4001)