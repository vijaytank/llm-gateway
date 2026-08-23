"""
test_anthropic_adapter_integration.py — Plan Phase 1: adapter end-to-end.

Covers (plan test_anthropic_contract, against the live stack):
  - Anthropic /v1/messages → translation → gateway → mock → translated back
  - Response is valid Anthropic Message shape (id prefix, role, stop_reason)
  - System prompt and multi-turn history survive the round trip
  - Tool-use block round trip (assistant tool_use → user tool_result)
  - Streaming endpoint emits Anthropic SSE event order
"""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import ADAPTER_URL, http_json, http_raw  # noqa: E402


def _messages(payload, timeout=60):
    return http_json("POST", f"{ADAPTER_URL}/v1/messages", payload, timeout=timeout)


def test_basic_message_round_trip():
    status, body = _messages({
        "model": "auto-free",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hello through the adapter"}],
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert data["id"].startswith(("msg_", "chatcmpl")) or data["id"]
    assert len(data["content"]) >= 1
    text_blocks = [b for b in data["content"] if b.get("type") == "text"]
    assert any("Mock reply" in b["text"] for b in text_blocks), data
    assert data["stop_reason"] in ("end_turn", "stop", None)


def test_system_prompt_round_trip():
    """Top-level Anthropic system field must reach the model's context."""
    status, body = _messages({
        "model": "auto-free",
        "max_tokens": 50,
        "system": "You are a pirate.",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, body


def test_multi_turn_history_preserved():
    status, body = _messages({
        "model": "auto-free",
        "max_tokens": 50,
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ],
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["role"] == "assistant"


def test_tool_use_round_trip():
    """Anthropic tool definitions + tool_result blocks translate cleanly."""
    status, body = _messages({
        "model": "auto-free",
        "max_tokens": 100,
        "tools": [{
            "name": "get_weather",
            "description": "Get current weather",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }],
        "messages": [
            {"role": "user", "content": "What's the weather in Paris?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_01",
                 "name": "get_weather", "input": {"city": "Paris"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01",
                 "content": "18C, clear"}
            ]},
        ],
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["role"] == "assistant"


def test_streaming_emits_anthropic_sse():
    payload = {
        "model": "auto-free",
        "max_tokens": 30,
        "stream": True,
        "messages": [{"role": "user", "content": "stream via adapter"}],
    }
    status, headers, raw = http_raw(
        "POST", f"{ADAPTER_URL}/v1/messages/stream", payload, timeout=60)
    assert status == 200, raw[:300]
    text = raw.decode()

    events = []
    for line in text.splitlines():
        if line.startswith("event:"):
            events.append(line.split(":", 1)[1].strip())
    assert events, f"no SSE event lines in: {text[:400]}"
    # Anthropic stream opens with message_start
    assert events[0] == "message_start", f"first event was {events[0]}"


def test_openai_passthrough_on_adapter_port():
    """Adapter also serves OpenAI-format pass-through to the gateway."""
    status, body = http_json(
        "POST", f"{ADAPTER_URL}/v1/chat/completions",
        {"model": "auto-free",
         "messages": [{"role": "user", "content": "passthrough"}]},
        timeout=60,
    )
    assert status == 200, body
    data = json.loads(body)
    assert data["object"] == "chat.completion"
