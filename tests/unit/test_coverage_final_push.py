"""Coverage: stream_reader reclaim + callbacks async hooks + translation streaming."""

import asyncio

import pytest

from brain.stream_reader import StreamReader
import gateway.callbacks as cb_mod
from gateway.callbacks import CustomLogger


@pytest.fixture
def reader(fake_redis):
    return StreamReader(redis_client=fake_redis)


def test_reclaim_processes_claimed_messages(reader, fake_redis):
    """XAUTOCLAIM path: pending message from a 'crashed' consumer (same group)
    gets claimed, processed (score written), and ACKed."""
    import time
    r = fake_redis
    reader._ensure_consumer_group()  # create brain:consumer group
    r.xadd("gateway:requests:stream", {
        "virtual_model": "reclaimed-m", "actual_model": "",
        "provider": "prov", "status": "success",
        "request_metadata": "{}", "response_metadata": "{}",
        "timestamp": "1.0",
    })
    # A 'crashed' consumer in the SAME group reads but never ACKs.
    r.xreadgroup(reader.consumer_group, "ghost-consumer",
                 {"gateway:requests:stream": ">"}, count=10)
    # Let the message go idle past the reclaim threshold.
    reader.claim_idle_ms = 200
    time.sleep(0.3)
    reclaimed = reader._reclaim_pending()
    assert reclaimed == 1
    assert float(r.get("gateway:model:reclaimed-m:score")) >= 0
    pending = r.xpending("gateway:requests:stream", reader.consumer_group)
    assert pending["pending"] == 0


def test_async_success_hook_offloads(fake_redis):
    """F-M8: async hooks complete without blocking and still write the event."""
    logger = CustomLogger(redis_client=fake_redis, postgres_dsn="postgresql://mock")

    class Usage:
        prompt_tokens = 5
        completion_tokens = 2

    class Resp:
        model = "prov/async-m"
        usage = Usage()

    asyncio.run(logger.async_log_success_event(
        {"model": "auto-free"}, Resp(), None, None))
    raw = fake_redis.xrange("gateway:requests:stream", count=10)
    assert len(raw) == 1
    assert dict(raw[0][1])["actual_model"] == "async-m"


def test_translate_anthropic_stream_to_openai_events():
    from adapter.translation import (
        translate_anthropic_stream_to_openai, translate_openai_stream_to_anthropic)

    anth_events = [
        {"type": "content_block_start", "data": "{}"},
        {"type": "content_block_delta",
         "data": '{"type": "text_delta", "delta": "hi"}'},
        {"type": "message_delta", "data": '{"stop_reason": "end_turn"}'},
        {"type": "message_stop", "stop_reason": "end_turn"},
    ]
    oai = translate_anthropic_stream_to_openai(anth_events)
    assert len(oai) >= 3

    # And the reverse direction produces a well-formed Anthropic sequence.
    oai_chunks = [
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "hey"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    anth = translate_openai_stream_to_anthropic(oai_chunks)
    kinds = [e["type"] for e in anth]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"


def test_tool_use_round_trip():
    from adapter.translation import (
        translate_anthropic_to_openai_request,
        translate_openai_to_anthropic_response)

    req = {
        "model": "auto-code-free", "max_tokens": 100,
        "messages": [{
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1",
                         "name": "get_weather",
                         "input": {"city": "Mumbai"}}],
        }],
    }
    oai = translate_anthropic_to_openai_request(req)
    tc = oai["messages"][0]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert '"city"' in tc["function"]["arguments"]

    resp = {
        "id": "r", "model": "m",
        "choices": [{"message": {"role": "assistant", "content": None,
                                 "tool_calls": [tc]},
                     "finish_reason": "tool_calls"}],
    }
    anth = translate_openai_to_anthropic_response(resp)
    block = next(b for b in anth["content"] if b["type"] == "tool_use")
    assert block["name"] == "get_weather"
    assert anth["stop_reason"] == "tool_use"
