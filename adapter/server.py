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
    translate_openai_stream_to_anthropic,
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


@app.post("/v1/messages")
async def anthropic_messages(request: AnthropicMessageRequest):
    """
    Handle Anthropic /v1/messages POST.
    
    Translates Anthropic request shape to OpenAI chat/completions format,
    forwards to gateway, then translates response back to Anthropic format.
    """
    try:
        # Step 1: Translate Anthropic request to OpenAI format
        oai_request = translate_anthropic_to_openai_request(request.dict())
        
        # Step 2: Forward to gateway using httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GATEWAY_BASE_URL}/v1/chat/completions",
                json=oai_request,
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
):
    """
    Handle Anthropic /v1/messages/stream POST with streaming.
    
    Translates Anthropic streaming events to OpenAI format and streams back.
    """
    try:
        # Step 1: Translate Anthropic request to OpenAI format
        oai_request = translate_anthropic_to_openai_request(request.dict())
        
        # Step 2: Forward to gateway
        async with httpx.AsyncClient() as client:
            # Stream the response from gateway
            async with client.stream(
                "POST",
                f"{GATEWAY_BASE_URL}/v1/chat/completions",
                json=oai_request,
                timeout=60.0,
            ) as gateway_response:
                # Translate and stream back as Anthropic events
                async def event_stream():
                    async for chunk in gateway_response.aiter_text():
                        # Parse and translate each chunk
                        if chunk:
                            try:
                                # Try to parse as SSE or JSON
                                translated = await translate_openai_stream_to_anthropic(
                                    [{"type": "chunk", "data": chunk}]
                                )
                                for ev in translated:
                                    yield f"data: {json.dumps(ev)}\n\n"
                            except Exception as e:
                                logger.warning(f"Translation error: {e}")
                                # Pass through raw chunk
                                yield f"data: {chunk}\n\n"
                
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
                timeout=60.0,
            )
        
        return response.json()
    
    except Exception as e:
        logger.error(f"Error forwarding OpenAI request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=4001)