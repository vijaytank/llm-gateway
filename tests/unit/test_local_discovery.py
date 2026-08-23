"""
tests/unit/test_local_discovery.py — Phase 3 local model discovery unit tests.

Uses respx-style mocking via httpx MockTransport-free approach: monkeypatch
the probe functions to return canned payloads shaped exactly like real
Ollama /api/tags and vLLM /v1/models responses.
"""

import pytest

from gateway.local_discovery import (
    DiscoveredModel,
    classify_capabilities,
    discover_local_models,
    sanitize_model_name,
)


def test_sanitize_names():
    assert sanitize_model_name("llama3:8b") == "local-llama3-8b"
    assert sanitize_model_name("qwen2.5-coder:7b-instruct-q4_K_M") == \
        "local-qwen2.5-coder-7b-instruct-q4_k_m"
    assert sanitize_model_name("meta-llama/Llama-3.1-8B") == "local-meta-llama-llama-3.1-8b"


def test_classify_code_model():
    caps = classify_capabilities("qwen2.5-coder:7b")
    assert "code" in caps
    assert "general" in caps


def test_classify_reasoning_model():
    caps = classify_capabilities("deepseek-r1:8b")
    assert "reasoning" in caps


def test_classify_general_only():
    caps = classify_capabilities("llama3:8b-instruct")
    assert caps == ["general"]


def test_discover_ollama(monkeypatch):
    from gateway import local_discovery as ld

    fake = [{
        "id": "llama3:8b",
        "size_bytes": 4_000_000_000,
        "details": {"family": "llama"},
    }, {
        "id": "qwen2.5-coder:7b",
        "size_bytes": 4_200_000_000,
        "details": {"family": "qwen2"},
    }]
    monkeypatch.setattr(ld, "probe_ollama", lambda url, timeout=5.0: fake)

    found = discover_local_models(ollama_base_url="http://localhost:11434")
    assert len(found) == 2
    names = {m.model_name for m in found}
    assert "local-llama3-8b" in names
    assert "local-qwen2.5-coder-7b" in names
    for m in found:
        assert m.provider == "ollama"
        assert m.base_url == "http://localhost:11434"


def test_discover_vllm(monkeypatch):
    from gateway import local_discovery as ld

    fake = [{"id": "mistralai/Mistral-7B-Instruct-v0.3", "context_window": 32768}]
    monkeypatch.setattr(ld, "probe_vllm", lambda url, timeout=5.0: fake)

    found = discover_local_models(vllm_base_url="http://localhost:8000")
    assert len(found) == 1
    m = found[0]
    assert m.provider == "vllm"
    assert m.upstream_name == "mistralai/Mistral-7B-Instruct-v0.3"
    assert m.context_window == 32768


def test_unreachable_endpoint_skipped_not_fatal(monkeypatch):
    from gateway import local_discovery as ld

    def boom(url, timeout=5.0):
        raise ConnectionError("refused")

    monkeypatch.setattr(ld, "probe_ollama", boom)
    monkeypatch.setattr(ld, "probe_vllm", boom)
    found = discover_local_models(
        ollama_base_url="http://localhost:11434",
        vllm_base_url="http://localhost:8000",
    )
    assert found == []


def test_disabled_local_skips_discovery():
    class FakeLocal:
        enabled = False
        probe_timeout_seconds = 12

    class FakeProviders:
        local = FakeLocal()
        ollama_base_url = "http://localhost:11434"
        vllm_base_url = ""

    class FakeConfig:
        providers = FakeProviders()

    from gateway.local_discovery import run_discovery
    assert run_discovery(FakeConfig()) == []


def test_register_upserts_into_sqlite(tmp_path, monkeypatch):
    """Registry upsert works against a scratch SQLite DB (dialect-portable schema)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from schemas.db import Base, ModelRegistry
    from gateway.local_discovery import register_discovered_models

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # Point the function at our engine's URL (sqlite memory won't survive a new
    # engine, so use a file DB).
    db_file = tmp_path / "registry.db"
    file_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(file_engine)
    file_engine.dispose()

    models = [
        DiscoveredModel(
            provider="ollama",
            model_name="local-llama3-8b",
            upstream_name="llama3:8b",
            base_url="http://localhost:11434",
            capabilities=["general"],
        ),
        DiscoveredModel(
            provider="vllm",
            model_name="local-mistral-7b",
            upstream_name="mistralai/Mistral-7B-Instruct-v0.3",
            base_url="http://localhost:8000",
            capabilities=["general"],
            context_window=32768,
        ),
    ]
    n = register_discovered_models(models, f"sqlite:///{db_file}")
    assert n == 2

    # Idempotent re-run updates rather than duplicating.
    n2 = register_discovered_models(models, f"sqlite:///{db_file}")
    assert n2 == 2

    eng = create_engine(f"sqlite:///{db_file}")
    with Session(eng) as session:
        rows = session.query(ModelRegistry).all()
        assert len(rows) == 2
        ollama_row = next(r for r in rows if r.provider == "ollama")
        assert ollama_row.tier == "free"
        assert ollama_row.source == "builtin"
        assert ollama_row.extra["litellm_name"] == "local-llama3-8b"
    eng.dispose()
