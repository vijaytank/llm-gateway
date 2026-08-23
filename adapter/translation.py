"""
adapter/translation.py — Anthropic ↔ OpenAI request/response translation

Core translation logic for the Anthropic Inbound Adapter. Handles:
- System prompt translation
- Tool use block translation
- Image content block translation
- Stop reason mapping
- Streaming event mapping

Based on patterns from opencode-provider-nvidia-nim/translate.go
"""

from typing import Any, Dict, List, Optional, Union
import json
import uuid
from pydantic import BaseModel

from adapter.schemas import AnthropicMessageResponse


class ContentBlock(BaseModel):
    """Represents an Anthropic content block."""
    type: str
    text: Optional[str] = None
    source: Optional[Dict[str, Any]] = None
    title: Optional[str] = None
    id: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    tool_use_id: Optional[str] = None
    is_error: Optional[bool] = None
    media_type: Optional[str] = None
    data: Optional[str] = None
    # Tool results carry their payload here in the Anthropic wire format
    content: Optional[Any] = None


class AnthropicMessage(BaseModel):
    """Represents an Anthropic message."""
    role: str
    content: Union[str, List[ContentBlock]]


class AnthropicRequest(BaseModel):
    """Represents an Anthropic Messages API request."""
    system: Optional[Union[str, List[ContentBlock]]] = None
    messages: List[AnthropicMessage]
    model: Optional[str] = None
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop: Optional[List[str]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    stream: bool = False
    temperature: Optional[float] = None


class OAIMessage(BaseModel):
    """Represents an OpenAI chat completion message."""
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class OAIChoice(BaseModel):
    """Represents an OpenAI choice."""
    index: int = 0
    delta: Dict[str, Any] = {}
    finish_reason: Optional[str] = None


class OAIStreamChunk(BaseModel):
    """Represents an OpenAI stream chunk."""
    id: str = "chatcmpl-123"
    object: str = "chat.completion.chunk"
    created: int = 0
    model: str = ""
    choices: List[OAIChoice] = []


class AnthropicStopReason:
    """Anthropic stop reason values (string constants)."""
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"


class OpenAIFinishReason:
    """OpenAI finish reason values (string constants)."""
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"


def translate_system_prompt(anthropic_system: Any) -> List[OAIMessage]:
    """
    Translate Anthropic system prompt to OpenAI messages format.
    
    Anthropic: top-level 'system' field
    OpenAI: first message with 'role': 'system'
    """
    if anthropic_system is None:
        return []
    
    if isinstance(anthropic_system, str):
        return [OAIMessage(role="system", content=anthropic_system)]
    
    # It's a list of content blocks
    messages = []
    for block in anthropic_system:
        if block.type == "text" and block.text:
            messages.append(OAIMessage(role="system", content=block.text))
        # Other block types are handled separately
    
    return messages


def translate_tool_use_block(anthropic_block: ContentBlock) -> Dict[str, Any]:
    """
    Translate Anthropic tool_use block to OpenAI tool_calls format.
    
    Anthropic: {'type': 'tool_use', 'id': 'toolu_123', 'name': 'get_weather', 'input': {...}}
    OpenAI: {'id': 'toolu_123', 'type': 'function', 'function': {'name': 'get_weather', 'arguments': '...'}}
    """
    if anthropic_block.type != "tool_use":
        return {}
    
    return {
        "id": anthropic_block.id or f"toolu_{id(anthropic_block)}",
        "type": "function",
        "function": {
            "name": anthropic_block.name or "unknown_tool",
            "arguments": json.dumps(anthropic_block.input or {}),
        },
    }


def translate_tool_result_block(anthropic_block: ContentBlock, role: str = "tool") -> Dict[str, Any]:
    """
    Translate Anthropic tool_result block to OpenAI tool role message.
    
    Anthropic: {'type': 'tool_result', 'tool_use_id': 'toolu_123', 'content': 'result', 'is_error': bool}
    OpenAI: {'role': 'tool', 'tool_call_id': 'toolu_123', 'content': 'result'}
    """
    if anthropic_block.type != "tool_result":
        return {}
    
    flat = _tool_result_text(anthropic_block)
    result = {
        "role": role,
        "tool_call_id": anthropic_block.tool_use_id or "",
        "content": f"[tool_error] {flat}" if anthropic_block.is_error else flat,
    }
    
    return result


def translate_image_block(anthropic_block: ContentBlock) -> Optional[Dict[str, Any]]:
    """
    Translate Anthropic image block to OpenAI image_url format.
    
    Anthropic: {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': '...'}}
    OpenAI: {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,...'}}
    """
    if anthropic_block.type != "image":
        return None
    
    source = anthropic_block.source or {}
    media_type = source.get("media_type", "image/jpeg")
    data = source.get("data", "")
    
    if not data:
        return None
    
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{media_type};base64,{data}",
        },
    }


def translate_stop_reason(anthropic_stop_reason: str) -> str:
    """
    Translate Anthropic stop reason to OpenAI finish reason.
    
    Mapping:
    - end_turn -> stop
    - tool_use -> tool_calls
    - max_tokens -> length
    """
    mapping = {
        AnthropicStopReason.END_TURN: OpenAIFinishReason.STOP,
        AnthropicStopReason.TOOL_USE: OpenAIFinishReason.TOOL_CALLS,
        AnthropicStopReason.MAX_TOKENS: OpenAIFinishReason.LENGTH,
    }
    return mapping.get(anthropic_stop_reason, OpenAIFinishReason.STOP)


def _tool_result_text(block: ContentBlock) -> str:
    """Flatten a tool_result content payload (str | list of blocks) to text."""
    payload = block.content
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = []
        for p in payload:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
            elif hasattr(p, "text") and p.text is not None:
                parts.append(p.text)
        return "\n".join(parts)
    return ""


def translate_anthropic_to_openai_request(
    anthropic_request: Union[AnthropicRequest, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Translate a full Anthropic request to OpenAI Chat Completions format.

    Accepts either an AnthropicRequest model or a raw dict.
    Returns a dict suitable as the request body for OpenAI-compatible endpoints.
    """
    if isinstance(anthropic_request, dict):
        anthropic_request = AnthropicRequest.model_validate(anthropic_request)
    # Translate system prompt
    system_messages = translate_system_prompt(anthropic_request.system)

    # Translate messages
    oai_messages: List[Dict[str, Any]] = []
    for msg in anthropic_request.messages:
        content = msg.content

        if isinstance(content, str):
            oai_messages.append({"role": msg.role, "content": content})
            continue

        tool_calls = []
        tool_results = []
        text_parts = []
        image_parts = []

        for block in content or []:
            if block.type == "text":
                text_parts.append(block.text or "")
            elif block.type == "image":
                img = translate_image_block(block)
                if img:
                    image_parts.append(img)
            elif block.type == "tool_use":
                tool_calls.append(translate_tool_use_block(block))
            elif block.type == "tool_result":
                tool_results.append(translate_tool_result_block(block))
            elif block.type == "thinking":
                if block.text:
                    text_parts.append(f"Thinking: {block.text}")

        # Tool result messages map 1:1 to OpenAI role=tool messages
        for tr_msg in tool_results:
            oai_messages.append(tr_msg)

        # Assistant message carrying tool_use → OpenAI assistant with tool_calls
        body: Dict[str, Any] = {"role": msg.role}
        if tool_calls:
            body["tool_calls"] = tool_calls
            combined_text = "\n".join(t for t in text_parts if t)
            body["content"] = combined_text or None
        elif len(image_parts) > 0:
            # Multimodal content: list of typed parts
            content_list = ([{"type": "text", "text": t} for t in text_parts if t]
                            + image_parts)
            body["content"] = content_list[0] if False else content_list
        else:
            body["content"] = "\n".join(t for t in text_parts if t) or None

        oai_messages.append(body)

    # Build OpenAI request — system prompt first (Issue 3 translation table),
    # then conversation messages
    oai_request = {
        "model": anthropic_request.model or "auto-free",
        "messages": (
            [m.dict(exclude_unset=True) for m in system_messages]
            + [m for m in oai_messages]
        ),
        "max_tokens": anthropic_request.max_tokens or 4096,
        "temperature": anthropic_request.temperature,
        "top_p": anthropic_request.top_p,
        "stream": anthropic_request.stream,
    }
    
    # Add tools if present — Anthropic tool schema → OpenAI function schema
    if anthropic_request.tools:
        oai_tools = []
        for t in anthropic_request.tools:
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema",
                                        {"type": "object", "properties": {}}),
                },
            })
        oai_request["tools"] = oai_tools
        if anthropic_request.tool_choice:
            oai_request["tool_choice"] = anthropic_request.tool_choice
    
    # Map stop sequences
    if anthropic_request.stop:
        oai_request["stop"] = anthropic_request.stop
    
    return oai_request


def translate_openai_to_anthropic_response(
    oai_response: Dict[str, Any],
) -> AnthropicMessageResponse:
    """
    Translate OpenAI Chat Completions response to Anthropic Messages API format.
    
    Handles:
    - Message role mapping
    - Content block reconstruction
    - Tool use translation
    - Stop reason mapping
    """
    choices = oai_response.get("choices", [])
    if not choices:
        return AnthropicMessageResponse(
            id=f"msg_{uuid.uuid4().hex[:24]}",
            role="assistant",
            model=oai_response.get("model", ""),
            content=[{"type": "text", "text": ""}],
            stop_reason=AnthropicStopReason.END_TURN,
        ).model_dump()
    
    choice = choices[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", AnthropicStopReason.END_TURN)
    
    # Translate content
    content = message.get("content", "")
    role = message.get("role", "assistant")
    
    # Translate tool calls if present
    tool_calls = message.get("tool_calls", [])
    anthropic_tool_calls = []
    for tc in tool_calls:
        anthropic_tool_calls.append({
            "id": tc.get("id", ""),
            "type": "tool_use",
            "name": tc.get("function", {}).get("name", "unknown"),
            "input": tc.get("function", {}).get("arguments", "{}"),
        })
    
    # Translate stop reason
    stop_reason = translate_stop_reason_from_openai(finish_reason, tool_calls=tool_calls)

    # Build Anthropic content blocks: text first, then tool_use blocks
    content_blocks: List[Dict[str, Any]] = []
    if isinstance(content, str) and content:
        content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        content_blocks.extend(content)
    for tc in anthropic_tool_calls:
        tool_input = tc.get("input", {})
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input or "{}")
            except json.JSONDecodeError:
                tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc.get("name", "unknown"),
            "input": tool_input,
        })
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    resp = AnthropicMessageResponse(
        id=oai_response.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        role="assistant",
        model=oai_response.get("model", ""),
        content=content_blocks,
        stop_reason=stop_reason,
    )
    usage = oai_response.get("usage") or {}
    if usage:
        resp.usage.input_tokens = int(usage.get("prompt_tokens") or 0)
        resp.usage.output_tokens = int(usage.get("completion_tokens") or 0)
    # Plain wire-format dict (FastAPI serializes either, but callers/tests
    # treat this as the raw Anthropic Messages response body)
    return resp.model_dump()


def translate_stop_reason_from_openai(
    finish_reason: str,
    tool_calls: List[Dict[str, Any]] = None,
) -> str:
    """
    Translate OpenAI finish reason to Anthropic stop reason.
    
    Mapping:
    - stop -> end_turn (if no tool calls) or tool_use (if tool calls present)
    - tool_calls -> tool_use
    - length -> max_tokens
    """
    if tool_calls and len(tool_calls) > 0:
        return AnthropicStopReason.TOOL_USE
    
    mapping = {
        "stop": AnthropicStopReason.END_TURN,
        "tool_calls": AnthropicStopReason.TOOL_USE,
        "length": AnthropicStopReason.MAX_TOKENS,
    }
    return mapping.get(finish_reason, AnthropicStopReason.END_TURN)


def translate_anthropic_stream_to_openai(
    anth_events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Translate Anthropic SSE events to OpenAI SSE format.
    
    Anthropic events: content_block_start, content_block_delta, message_delta, message_stop
    OpenAI events: choices.delta, finish_reason
    
    This is a simplified mapping for the adapter.
    """
    oai_events = []
    state = {
        "tool_calls": [],
        "text": "",
        "thinking": "",
        "current_block": None,
        "block_index": 0,
        "started": False,
    }
    
    for event in anth_events:
        event_type = event.get("type", "")
        data = event.get("data", "")
        
        if event_type == "content_block_start":
            state["started"] = True
            state["current_block"] = "text"  # default
            state["block_index"] = 0
            oai_events.append({"event": "message_start", "data": {"role": "assistant"}})
        
        elif event_type == "content_block_delta":
            delta = json.loads(data) if isinstance(data, str) else data
            delta_type = delta.get("type", "")
            
            if delta_type == "text_delta":
                state["text"] += delta.get("delta", "")
                oai_events.append({
                    "event": "delta",
                    "data": {"delta": {"content": delta.get("delta", "")}, "index": 0},
                })
            elif delta_type == "input_json_delta":
                # Tool call arguments
                idx = delta.get("index", 0)
                if "partial_json" in delta:
                    # Accumulate tool call args
                    if idx < len(state["tool_calls"]):
                        state["tool_calls"][idx]["function"]["arguments"] += delta["partial_json"]
                    else:
                        state["tool_calls"].append({
                            "id": f"toolu_{state['block_index']}",
                            "type": "function",
                            "function": {"name": "", "arguments": delta["partial_json"]},
                        })
                state["block_index"] += 1
            elif delta_type == "thinking_delta":
                state["thinking"] += delta.get("delta", "")
            
            oai_events.append({
                "event": "choices.delta",
                "data": {"delta": {"content": delta.get("delta", "")}, "index": 0},
            })
        
        elif event_type == "message_delta":
            # Final delta - may include usage
            usage = json.loads(data) if isinstance(data, str) else {}
            oai_events.append({
                "event": "delta",
                "data": {"delta": {}, "index": 0, "finish_reason": usage.get("stop_reason")},
            })
        
        elif event_type == "message_stop":
            finish_reason = event.get("stop_reason", "end_turn")
            oai_events.append({
                "event": "finish_reason",
                "data": {"finish_reason": finish_reason},
            })
    
    return oai_events


def translate_openai_stream_to_anthropic(
    oai_chunks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Translate OpenAI SSE chunks to Anthropic SSE format.
    
    OpenAI events: choices.delta, finish_reason
    Anthropic events: content_block_start, content_block_delta, message_delta, message_stop
    
    This is a simplified mapping for the adapter.
    """
    anth_events = []   # each: {"type": <anthropic sse event>, ...payload}
    state = {
        "tool_calls": [],
        "text": "",
        "thinking": "",
        "block_index": 0,
        "text_started": False,
        "thinking_started": False,
        "tool_started": False,
        "message_started": False,
    }
    
    for chunk in oai_chunks:
        choices = chunk.get("choices", [])
        if not choices:
            continue
        
        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")
        
        # Handle tool calls
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                idx = tc.get("index", 0)
                if idx >= len(state["tool_calls"]):
                    state["tool_calls"].append({
                        "id": f"toolu_{state['block_index']}",
                        "type": "tool_use",
                        "name": tc.get("function", {}).get("name", ""),
                        "input": tc.get("function", {}).get("arguments", ""),
                    })
                else:
                    if "function" in tc and "arguments" in tc["function"]:
                        if idx < len(state["tool_calls"]):
                            state["tool_calls"][idx]["function"]["arguments"] += tc["function"]["arguments"]
                
                if not state["message_started"]:
                    anth_events.append({"type": "message_start",
                                        "message": {"role": "assistant"}})
                    state["message_started"] = True
                if not state["tool_started"]:
                    anth_events.append({
                        "type": "content_block_start",
                        "index": state["block_index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": state["tool_calls"][-1]["id"],
                            "name": state["tool_calls"][-1]["name"],
                            "input": {},
                        },
                    })
                    state["tool_started"] = True
        
        # Handle text content
        if delta.get("content"):
            content = delta["content"]
            if not state["message_started"]:
                anth_events.append({"type": "message_start",
                                    "message": {"role": "assistant"}})
                state["message_started"] = True
            if not state["text_started"]:
                anth_events.append({
                    "type": "content_block_start",
                    "index": state["block_index"],
                    "content_block": {"type": "text", "text": ""},
                })
                state["text_started"] = True
            state["text"] += content
            anth_events.append({
                "type": "content_block_delta",
                "index": state["block_index"],
                "delta": {"type": "text_delta", "text": content},
            })
        
        # Handle thinking/thought blocks
        if delta.get("thinking"):
            if not state["thinking_started"]:
                anth_events.append({
                    "type": "content_block_start",
                    "index": state["block_index"],
                    "content_block": {"type": "thinking", "thinking": ""},
                })
                state["thinking_started"] = True
            state["thinking"] += delta["thinking"]
            anth_events.append({
                "type": "content_block_delta",
                "index": state["block_index"],
                "delta": {"type": "thinking_delta", "thinking": delta["thinking"]},
            })
        
        # Handle finish/reason
        if finish_reason:
            stop_reason = translate_stop_reason_from_openai(finish_reason, tool_calls=state["tool_calls"])
            anth_events.append({
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            })
            anth_events.append({"type": "message_stop"})
            break
    
    return anth_events