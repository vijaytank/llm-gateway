"""Interactive-helper coverage for wizard/setup.py (F-M15 final push)."""

import pytest

from wizard import setup as wiz


def test_get_yes_no_defaults(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert wiz.get_yes_no("q?", default=True) is True
    assert wiz.get_yes_no("q?", default=False) is False


def test_get_yes_no_rejects_garbage_then_accepts(monkeypatch):
    answers = iter(["maybe", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert wiz.get_yes_no("q?", default=True) is True


def test_prompt_with_default(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "  ")
    assert wiz.prompt_with_default("q?", "fallback") == "fallback"
    monkeypatch.setattr("builtins.input", lambda *_: "typed")
    assert wiz.prompt_with_default("q?", "fallback") == "typed"


def test_choose_providers_toggles(monkeypatch):
    from schemas.config import create_default_config
    cfg = create_default_config()
    answers = iter(["n", "n", "n", "n"])  # disable all four
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    wiz.choose_providers(cfg)
    assert cfg.providers.nvidia.enabled is False
    assert cfg.providers.groq.enabled is False
    assert cfg.providers.cerebras.enabled is False
    assert cfg.providers.openrouter.enabled is False


def test_choose_local_models_enable_with_urls(monkeypatch):
    from schemas.config import create_default_config
    cfg = create_default_config()
    answers = iter(["y", "", "http://my-vllm:8000"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    wiz.choose_local_models(cfg)
    assert cfg.providers.local.enabled is True
    assert cfg.providers.vllm_base_url == "http://my-vllm:8000"


def test_choose_deployment_mode_validation(monkeypatch):
    answers = iter(["kubernetes", "bare-metal"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert wiz.choose_deployment_mode() == "bare-metal"
