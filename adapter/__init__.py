"""
adapter/__init__.py — Anthropic Inbound Adapter package
Minimal FastAPI service translating Anthropic /v1/messages to OpenAI format
and back, sitting on port 4001 in front of the main gateway on port 4000.
"""

__version__ = "1.0.0"

# Export key translation functions for testing
from adapter.translation import (
    translate_anthropic_to_openai_request,
    translate_openai_to_anthropic_response,
    translate_anthropic_stream_to_openai,
    translate_openai_stream_to_anthropic,
)
from adapter.server import app, router
from adapter.schemas import (
    AnthropicMessageRequest,
    AnthropicMessageResponse,
    OpenAIChatCompletion,
    OpenAIChatCompletionChunk,
)

__all__ = [
    "app",
    "router",
    "translate_anthropic_to_openai_request",
    "translate_openai_to_anthropic_response",
    "translate_anthropic_stream_to_openai",
    "translate_openai_stream_to_anthropic",
    "AnthropicMessageRequest",
    "AnthropicMessageResponse",
]