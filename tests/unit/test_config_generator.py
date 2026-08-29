"""Phase 1 unit tests: gateway/config_generator.py (plan test_config_generator.py).

Given a known GatewayConfig + registry, the generated LiteLLM YAML has correct
model_list, router_settings, and fallbacks.

The generator reads the DB model_registry (single source of truth). Tests
monkeypatch _registry_models() with a fixed in-memory registry — no live DB.
"""

import pytest

from schemas.config import create_default_config
from gateway import config_generator as cg
from gateway.config_generator import (
    generate_litellm_config,
    get_models_from_registry,
    write_litellm_config,
)


# Fixed registry matching scripts/seed_model_registry.py BUILTIN_MODELS
SEED_REGISTRY = [
    {"provider": "nvidia", "model_name": "meta/llama-3.1-8b-instruct",
     "tier": "free", "capabilities": ["general"], "rpm": 40},
    {"provider": "nvidia", "model_name": "meta/llama-3.3-70b-instruct",
     "tier": "free", "capabilities": ["general", "reasoning"], "rpm": 40},
    {"provider": "nvidia", "model_name": "qwen/qwen2.5-coder-32b-instruct",
     "tier": "free", "capabilities": ["code"], "rpm": 40},
    {"provider": "nvidia", "model_name": "deepseek-ai/deepseek-r1",
     "tier": "free", "capabilities": ["reasoning"], "rpm": 40},
    {"provider": "groq", "model_name": "llama-3.1-8b-instant",
     "tier": "free", "capabilities": ["general"], "rpm": 30},
    {"provider": "groq", "model_name": "llama-3.3-70b-versatile",
     "tier": "free", "capabilities": ["general", "reasoning"], "rpm": 30},
    {"provider": "groq", "model_name": "qwen-2.5-coder-32b",
     "tier": "free", "capabilities": ["code"], "rpm": 30},
    {"provider": "cerebras", "model_name": "llama3.1-8b",
     "tier": "free", "capabilities": ["general"], "rpm": 30},
    {"provider": "cerebras", "model_name": "qwen-2.5-32b",
     "tier": "free", "capabilities": ["general", "reasoning"], "rpm": 30},
    {"provider": "openrouter", "model_name": "meta-llama/llama-3.1-8b-instruct:free",
     "tier": "free", "capabilities": ["general"], "rpm": 20},
]


@pytest.fixture(autouse=True)
def seeded_registry(monkeypatch):
    monkeypatch.setattr(cg, "_registry_models", lambda: SEED_REGISTRY)
    # P1.2.3: provider keys now resolve through gateway.credentials with a
    # process cache — clear it between tests so env overrides are observed.
    from gateway import credentials as creds
    creds.invalidate_cache()


def test_model_list_contains_all_enabled_providers():
    config = create_default_config()
    models = get_models_from_registry(config)
    names = {m["model_name"] for m in models}
    assert "nvidia-auto" in names
    assert "groq-auto-free" in names
    assert "cerebras-auto-free" in names
    assert "cerebras-reasoning-free" in names
    assert "openrouter-free" in names


def test_virtual_models_are_model_groups():
    """Virtual names must be model GROUPS: multiple deployments share the
    virtual name so LiteLLM routes/fails over across the chain natively.
    (A bare name without litellm_params is rejected by LiteLLM — found live
    in Docker.)"""
    config = create_default_config()
    cfg = generate_litellm_config(config)
    groups = {}
    for m in cfg["model_list"]:
        if m["model_name"].startswith("auto-"):
            assert "model" in m["litellm_params"], "group member missing upstream model"
            groups.setdefault(m["model_name"], []).append(m)
    for vm_name in ("auto-free", "auto-code-free", "auto-reasoning-free"):
        members = groups.get(vm_name, [])
        assert len(members) >= 2, f"{vm_name} group should span multiple providers"
        # every chain entry that exists as a deployment is represented in the group
        vm = next(v for v in config.virtual_models if v.name == vm_name)
        expected = [n for n in vm.fallback_chain
                    if n in {m["model_name"] for m in cfg["model_list"]}
                    and not m_is_virtual(n, cfg)]
        got = {m["litellm_params"]["model"] for m in members}
        assert len(members) == len(expected), f"{vm_name}: expected {expected}, got {got}"


def m_is_virtual(name: str, cfg) -> bool:
    """A name is virtual if it starts with auto- (the client-facing groups)."""
    return name.startswith("auto-")


def test_openrouter_is_last_resort_with_headers():
    """Issue 8 fix: OpenRouter free has required headers and last-resort flag."""
    config = create_default_config()
    models = get_models_from_registry(config)
    or_entries = [m for m in models if m["model_name"] == "openrouter-free"]
    assert or_entries, "openrouter-free entry missing"
    params = or_entries[0]["litellm_params"]
    assert params["extra_headers"]["HTTP-Referer"] == "http://localhost:4000"
    assert params["extra_headers"]["X-Title"] == "llm-gateway"
    assert or_entries[0]["custom_router_settings"]["last_resort"] is True


def test_disabled_providers_are_excluded():
    config = create_default_config()
    config.providers.groq.enabled = False
    models = get_models_from_registry(config)
    names = {m["model_name"] for m in models}
    assert not any(n.startswith("groq") for n in names)


def test_every_deployment_has_real_upstream_model():
    """Every litellm_params must carry a real upstream `model` — LiteLLM's
    proxy rejects deployments without it (live Docker finding)."""
    config = create_default_config()
    models = get_models_from_registry(config)
    deployments = [m for m in models if "litellm_params" in m]
    assert deployments
    for m in deployments:
        assert m["litellm_params"].get("model"), f"{m['model_name']} missing model param"


def test_api_keys_come_from_env_not_config(monkeypatch):
    """Provider keys resolved from env at generate time; absent env → no key."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_123")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    config = create_default_config()
    models = get_models_from_registry(config)
    groq = next(m for m in models if m["model_name"] == "groq-auto-free")
    nvidia = next(m for m in models if m["model_name"] == "nvidia-auto")
    assert groq["litellm_params"]["api_key"] == "gsk_test_123"
    assert "api_key" not in nvidia["litellm_params"]
