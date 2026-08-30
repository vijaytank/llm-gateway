"""Unit tests for gateway/callbacks.py — LiteLLM hook interface coverage.

Covers _model_info extraction, latency computation, and the pre-call routing
hook (review F-H1 wiring) with fakeredis-backed RouterHook state.
"""

import pytest

import gateway.callbacks as cb_mod
from gateway.callbacks import CustomLogger


@pytest.fixture
def logger(fake_redis):
    log = CustomLogger(redis_client=fake_redis, postgres_dsn="postgresql://mock")
    return log


# ---------------------------------------------------------------------------
# _model_info / _latency_ms
# ---------------------------------------------------------------------------

def test_model_info_prefers_response_model(logger):
    class Resp:
        model = "nvidia/meta/llama-3.1-8b"
    info = logger._model_info({"model": "auto-free"}, response_obj=Resp())
    assert info["actual_model"] == "llama-3.1-8b"  # prefix stripped
    assert info["virtual_model"] == "auto-free"


def test_model_info_falls_back_to_params(logger):
    class LP(dict):
        pass
    kwargs = {"model": "auto-free", "litellm_params": {"model": "groq/llama-3.1-8b"}}
    info = logger._model_info(kwargs)
    assert info["actual_model"] == "llama-3.1-8b"


def test_model_info_handles_empty_kwargs(logger):
    info = logger._model_info({})
    assert info["virtual_model"] == ""
    assert info["actual_model"] == ""


def test_latency_ms_computation(logger):
    from datetime import datetime, timedelta
    start = datetime(2025, 1, 1, 0, 0, 0)
    end = start + timedelta(milliseconds=1234)
    assert logger._latency_ms(start, end) == 1234
    assert logger._latency_ms(None, end) == 0


# ---------------------------------------------------------------------------
# async_pre_call_hook (F-H1: RouterHook wired into the request path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_call_hook_annotates_influence(logger, fake_redis):
    fake_redis.set("gateway:model:m-good:score", "0.9")
    data = {"model": "m-good"}
    out = await logger.async_pre_call_hook(None, None, data, "chat_completion")
    assert out["metadata"]["gateway_influence"] >= 0
    assert "gateway_model_excluded" not in out["metadata"]


@pytest.mark.asyncio
async def test_pre_call_hook_marks_excluded_when_all_circuits_open(logger, fake_redis):
    fake_redis.set("gateway:model:m-open:circuit", "open")
    fake_redis.set("gateway:model:auto-free:circuit", "open")
    data = {"model": "m-open"}
    out = await logger.async_pre_call_hook(None, None, data, "chat_completion")
    assert out["metadata"]["gateway_influence"] == -1
    assert out["metadata"]["gateway_model_excluded"] is True
    assert out["metadata"]["gateway_routing_reason"] == "circuit_open"


@pytest.mark.asyncio
async def test_pre_call_hook_reroutes_circuited_model(logger, fake_redis):
    """When a model is circuited/EOL, the pre-call hook automatically reroutes to its capability group."""
    fake_redis.set("gateway:model:meta/llama-3.3-70b-instruct:circuit", "open")
    data = {"model": "meta/llama-3.3-70b-instruct"}
    out = await logger.async_pre_call_hook(None, None, data, "chat_completion")
    assert out["model"] == "auto-reasoning-free"
    assert out["metadata"]["gateway_influence"] >= 0


@pytest.mark.asyncio
async def test_pre_call_hook_fails_safe_on_redis_errors(logger):
    """Redis unreachable → request proceeds untouched (fail-safe)."""
    logger._router_hook_instance = False  # sentinel forces re-init attempt
    # Point redis at None so the hook cannot consult anything.
    old = logger.redis
    logger.redis = None
    try:
        data = {"model": "whatever"}
        out = await logger.async_pre_call_hook(None, None, data, "chat_completion")
        assert out is data  # unchanged, no exception
    finally:
        logger.redis = old


# ---------------------------------------------------------------------------
# record paths through the public hooks
# ---------------------------------------------------------------------------

def test_log_success_event_writes_stream(logger, fake_redis):
    class Usage:
        prompt_tokens = 7
        completion_tokens = 3

    class Resp:
        model = "prov/m1"
        usage = Usage()

    logger.log_success_event({"model": "auto-free"}, Resp(), None, None)
    raw = fake_redis.xrange("gateway:requests:stream", count=10)
    assert len(raw) == 1
    fields = dict(raw[0][1])
    assert fields["status"] == "success"
    assert fields["input_tokens"] == "7"


def test_hook_exceptions_never_propagate(logger):
    class Boom:
        model = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))

    # Must not raise even when internals blow up
    logger.log_success_event(Boom(), None, None, None)
    logger.log_failure_event(Boom(), None, None, None)


def test_module_level_custom_logger_exists():
    assert isinstance(cb_mod.custom_logger, CustomLogger)
