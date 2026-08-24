"""Unit tests for gateway/config_generator.py edge paths — F-M15."""

import os

import pytest

from schemas.config import create_default_config
from gateway import config_generator as cg


def _cfg_with_local():
    cfg = create_default_config()
    cfg.providers.local.enabled = True
    return cfg


def test_registry_models_empty_without_db(monkeypatch):
    monkeypatch.delenv("GATEWAY_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cg._registry_models() == []


def test_generated_config_includes_routing_strategy():
    cfg = create_default_config()
    out = cg.generate_litellm_config(cfg)
    assert out["router_settings"]["routing_strategy"] == "latency-based-routing-v2"
    # Callback registration present (F-H1 wiring)
    assert "gateway.callbacks.custom_logger" in out["litellm_settings"]["callbacks"]


def test_write_litellm_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_URL", "")  # no DB → empty registry path
    monkeypatch.delenv("GATEWAY_DB_URL", raising=False)
    cfg = create_default_config()
    out_path = tmp_path / "litellm.yaml"
    cg.write_litellm_config(cfg, str(out_path))
    content = out_path.read_text()
    assert "model_list" in content
    assert "routing_strategy" in content


def test_custom_provider_placeholder_never_emitted(monkeypatch):
    """F-M7: schema rejects sentinels before the generator ever sees them."""
    from schemas.config import CustomProviderConfig
    with pytest.raises(Exception):
        CustomProviderConfig(
            name="x", base_url="http://up/v1",
            models=["__unverified__"])
