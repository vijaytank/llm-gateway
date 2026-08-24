"""Unit tests for gateway/local_discovery.py — F-M15 coverage."""

import pytest

import gateway.local_discovery as ld


def test_classify_capabilities_code():
    caps = ld.classify_capabilities("qwen2.5-coder-32b-instruct")
    assert "code" in caps and "general" in caps


def test_classify_capabilities_reasoning():
    caps = ld.classify_capabilities("deepseek-r1:8b")
    assert "reasoning" in caps and "general" in caps


def test_classify_capabilities_general_only():
    assert ld.classify_capabilities("llama3:8b") == ["general"]


def test_sanitize_model_name():
    assert ld.sanitize_model_name("llama3:8b-instruct-q4") == "local-llama3-8b-instruct-q4"
    # Weird chars collapse to single dashes
    out = ld.sanitize_model_name("weird//model::name")
    assert out.startswith("local-")
    assert "//" not in out and "::" not in out


def test_discover_unreachable_endpoints_not_fatal():
    """Unreachable Ollama/vLLM endpoints are skipped, not fatal."""
    found = ld.discover_local_models(
        ollama_base_url="http://127.0.0.1:1",
        vllm_base_url="http://127.0.0.1:2",
        timeout=0.5,
    )
    assert found == []


def test_run_discovery_disabled_is_noop(monkeypatch):
    cfg = type("C", (), {"providers": type(
        "P", (), {"local": type("L", (), {"enabled": False})()})()})()
    assert ld.run_discovery(cfg) == []
