"""
adapter/schemas.py — Pydantic request/response contracts for the Anthropic adapter

These mirror the wire formats so FastAPI can validate inbound Anthropic
requests and we can build well-typed OpenAI/Anthropic responses.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------- Anthropic inbound ----------

class AnthropicMessageRequest(BaseModel):
    """POST /v1/messages request body (Anthropic Messages API shape)."""
    model: str
    max_tokens: int = Field(gt=0)
    messages: List[Dict[str, Any]]
    system: Optional[Any] = None  # str or list of content blocks
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    stream: bool = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class AnthropicUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicMessageResponse(BaseModel):
    """Non-streaming /v1/messages response body."""
    id: str
    type: str = "message"
    role: str = "assistant"
    model: str
    content: List[Dict[str, Any]] = []
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)


# ---------- OpenAI upstream ----------

class OpenAIChatCompletion(BaseModel):
    """Non-streaming OpenAI /v1/chat/completions response (subset we consume)."""
    id: str
    object: str = "chat.completion"
    created: int = 0
    model: str
    choices: List[Dict[str, Any]] = []
    usage: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class OpenAIChatCompletionChunk(BaseModel):
    """Streaming OpenAI SSE chunk (subset we consume)."""
    id: str
    object: str = "chat.completion.chunk"
    created: int = 0
    model: str
    choices: List[Dict[str, Any]] = []

    model_config = {"extra": "allow"}


class AnthropicErrorBody(BaseModel):
    type: str = "error"
    error: Dict[str, str]
