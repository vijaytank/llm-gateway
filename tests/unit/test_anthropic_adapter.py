"""Phase 1 unit tests: adapter/translation.py (plan test_anthropic_adapter.py).

Covers message translation, tool-use, tool results, images, stop-reason
mapping, and streaming event order.
"""

import json

import pytest

from adapter import translation as tr


def _anthropic_request(**overrides):
    req = {
        "model": "claude-3-haiku",
        "max_tokens": 256,
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Hello"},
        ],
    }
    req.update(overrides)
    return req


# ---------- request translation ----------

def test_system_prompt_becomes_first_message():
    oai = tr.translate_anthropic_to_openai_request(_anthropic_request())
    assert oai["messages"][0]["role"] == "system"
    assert oai["messages"][0]["content"] == "You are helpful."
    assert oai["messages"][1] == {"role": "user", "content": "Hello"}
    assert "system" not in oai  # top-level system removed


def test_user_message_preserved():
    oai = tr.translate_anthropic_to_openai_request(_anthropic_request())
    user_msgs = [m for m in oai["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "Hello"


def test_tool_use_block_translated_to_tool_calls():
    req = _anthropic_request(messages=[
        {"role": "user", "content": "What's the weather?"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
             "input": {"city": "Paris"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": [{"type": "text", "text": "22C sunny"}]},
        ]},
    ])
    oai = tr.translate_anthropic_to_openai_request(req)

    assistant = next(m for m in oai["messages"]
                     if m.get("role") == "assistant" and m.get("tool_calls"))
    call = assistant["tool_calls"][0]
    assert call["id"] == "toolu_1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    # OpenAI arguments are a JSON string — parseable back to the input dict
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}

    tool_msg = next(m for m in oai["messages"] if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "toolu_1"
    assert "22C" in str(tool_msg["content"])


def test_image_content_translated_to_image_url():
    req = _anthropic_request(messages=[{
        "role": "user",
        "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": "aGVsbG8="}},
            {"type": "text", "text": "What is this?"},
        ],
    }])
    oai = tr.translate_anthropic_to_openai_request(req)
    content = oai["messages"][-1]["content"]
    img_part = next(p for p in content if p["type"] == "image_url")
    assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
    text_part = next(p for p in content if p["type"] == "text")
    assert text_part["text"] == "What is this?"


def test_tools_schema_translated():
    req = _anthropic_request(tools=[{
        "name": "get_weather",
        "description": "Get weather",
        "input_schema": {"type": "object", "properties": {
            "city": {"type": "string"}}},
    }])
    oai = tr.translate_anthropic_to_openai_request(req)
    assert oai["tools"][0]["type"] == "function"
    fn = oai["tools"][0]["function"]
    assert fn["name"] == "get_weather"
    assert fn["parameters"]["properties"]["city"]["type"] == "string"


# ---------- response translation ----------

def test_response_basic_shape():
    oai_resp = {
        "id": "chatcmpl-123",
        "model": "nvidia-auto",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hi there"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }
    anth = tr.translate_openai_to_anthropic_response(oai_resp)
    assert anth["id"]
    assert anth["role"] == "assistant"
    assert isinstance(anth["content"], list)
    assert anth["content"][0] == {"type": "text", "text": "Hi there"}
    assert anth["stop_reason"] == "end_turn"
    assert anth["usage"]["input_tokens"] == 5
    assert anth["usage"]["output_tokens"] == 7


@pytest.mark.parametrize("finish,expected", [
    ("stop", "end_turn"),
    ("tool_calls", "tool_use"),
    ("length", "max_tokens"),
])
def test_stop_reason_mapping(finish, expected):
    oai_resp = {
        "id": "x", "model": "m",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "y"},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    anth = tr.translate_openai_to_anthropic_response(oai_resp)
    assert anth["stop_reason"] == expected


def test_response_with_tool_call_maps_to_tool_use():
    oai_resp = {
        "id": "x", "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "f", "arguments": "{\"a\": 1}"},
            }]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    anth = tr.translate_openai_to_anthropic_response(oai_resp)
    block = next(b for b in anth["content"] if b["type"] == "tool_use")
    assert block["id"] == "call_1"
    assert block["name"] == "f"
    assert block["input"] == {"a": 1}   # JSON string parsed to dict
    assert anth["stop_reason"] == "tool_use"


# ---------- streaming ----------

def test_streaming_emits_valid_event_order():
    """Given a mocked OpenAI SSE stream, adapter emits valid Anthropic SSE events."""
    chunks = []
    for delta_text in ["Hel", "lo ", "world"]:
        chunks.append({
            "choices": [{"index": 0, "delta": {"content": delta_text},
                         "finish_reason": None}],
        })
    chunks.append({"choices": [{"index": 0, "delta": {},
                                "finish_reason": "stop"}]})

    events = tr.translate_openai_stream_to_anthropic(chunks)
    types = [e["type"] for e in events]

    assert types[0] == "message_start"
    assert "content_block_start" in types
    # all content_block_delta come after content_block_start
    assert types.index("content_block_start") < types.index("content_block_delta")
    # message_delta (with stop reason) then message_stop at the end
    assert types[-2:] == ["message_delta", "message_stop"]

    text = "".join(e["delta"]["text"] for e in events
                   if e["type"] == "content_block_delta")
    assert text == "Hello world"

    stop_ev = next(e for e in events if e["type"] == "message_delta")
    assert stop_ev["delta"]["stop_reason"] == "end_turn"
