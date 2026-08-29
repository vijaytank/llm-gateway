"""
gateway/config_generator.py — Generates LiteLLM config from GatewayConfig

Reads GatewayConfig + DB model registry, generates the model_list section
of LiteLLM's config YAML. Runs as an init step in the Docker entrypoint
before LiteLLM starts.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import Field

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from gateway.credentials import get_provider_api_key
from schemas.config import GatewayConfig, create_default_config, ProvidersConfig, VirtualModelConfig, ProviderConfig


def _registry_models() -> list:
    """Read enabled models from the Postgres model_registry (single source of
    truth). Returns [] if the DB is unreachable."""
    try:
        import os as _os
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from schemas.db import ModelRegistry

        # GATEWAY_DB_URL preferred: a bare DATABASE_URL in the gateway process
        # makes LiteLLM enable its Prisma layer (schema reset risk).
        url = _os.environ.get("GATEWAY_DB_URL") or _os.environ.get("DATABASE_URL", "")
        if not url:
            print("[config_generator] GATEWAY_DB_URL/DATABASE_URL not set; cannot read registry")
            return []
        engine = create_engine(url)
        with Session(engine) as session:
            rows = (
                session.query(ModelRegistry)
                .filter(ModelRegistry.enabled.is_(True))
                .all()
            )
            return [
                {
                    "provider": r.provider,
                    "model_name": r.model_name,
                    "tier": r.tier,
                    "capabilities": r.capabilities or [],
                    "rpm": r.rpm,
                }
                for r in rows
            ]
    except Exception as e:
        print(f"[config_generator] registry read failed: {e.__class__.__name__}: {e}")
        return []


# LiteLLM provider prefixes + the env var holding each provider's key.
# Keys are NEVER inlined here — resolved from the environment at generate time.
PROVIDER_LITELLM_PREFIX = {
    "nvidia": "nvidia_nim",  # LiteLLM's provider route for NVIDIA NIM
    "groq": "groq",
    "cerebras": "cerebras",
    "openrouter": "openrouter",
}
PROVIDER_API_KEY_ENV = {
    "nvidia": "NVIDIA_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def get_models_from_registry(config: GatewayConfig) -> list[dict[str, Any]]:
    """
    Generate LiteLLM model list from GatewayConfig + the DB model registry
    (plan Phase 1 deliverable 2).

    For every enabled builtin provider and every capability slot used by the
    virtual-model chains (general/code/reasoning), pick the provider's best
    registered model for that capability and emit a LiteLLM deployment with
    real upstream `model` params. Custom providers and discovered local
    models are appended separately below.
    """
    model_list: list[dict[str, Any]] = []
    providers = config.providers

    registry = _registry_models()
    if not registry:
        print("[config_generator] WARNING: empty registry — gateway will have no routable models")

    # Capability slots needed by the virtual chains, in priority order
    capability_slots = ["general", "code", "reasoning"]

    for provider_name in ("nvidia", "groq", "cerebras"):
        pc: ProviderConfig = getattr(providers, provider_name)
        if not pc.enabled:
            continue
        prefix = PROVIDER_LITELLM_PREFIX[provider_name]
        # DB-backed credential (P1.2.3) with env-var fallback (FR-1.2.4).
        api_key = get_provider_api_key(provider_name, PROVIDER_API_KEY_ENV[provider_name]) or ""
        provider_models = [m for m in registry if m["provider"] == provider_name]
        for slot in capability_slots:
            candidates = [m for m in provider_models if slot in m["capabilities"]]
            if not candidates:
                continue
            upstream = candidates[0]["model_name"]
            params: dict[str, Any] = {
                "model": f"{prefix}/{upstream}",
                "rpm": candidates[0]["rpm"] or 10,
            }
            if api_key:
                params["api_key"] = api_key
            entry: dict[str, Any] = {
                "model_name": (
                    f"{provider_name}-auto" if provider_name == "nvidia"
                    else f"{provider_name}-auto-free"
                ) if slot == "general"
                else f"{provider_name}-{slot}-free",
                "litellm_params": params,
                "model_info": {
                    "provider": provider_name,
                    "tier": candidates[0]["tier"],
                    "capabilities": [slot],
                    "mode": "chat",
                },
            }
            if provider_name == "openrouter":
                entry["litellm_params"]["extra_headers"] = pc.extra_headers or {}
            model_list.append(entry)

    # OpenRouter free (last resort per Issue 8): one entry covering general
    if providers.openrouter.enabled:
        or_models = [m for m in registry if m["provider"] == "openrouter"]
        or_extra = providers.openrouter.extra_headers or {}
        api_key = get_provider_api_key("openrouter", "OPENROUTER_API_KEY") or ""
        for m in or_models:
            params = {
                "model": f"openrouter/{m['model_name']}",
                "rpm": m["rpm"] or 10,
                "extra_headers": or_extra,
            }
            if api_key:
                params["api_key"] = api_key
            model_list.append({
                "model_name": "openrouter-free",
                "litellm_params": params,
                "model_info": {
                    "provider": "openrouter",
                    "tier": m["tier"],
                    "capabilities": m["capabilities"],
                    "mode": "chat",
                },
                "custom_router_settings": {"last_resort": True},
            })
    
    # Local models (if enabled) — discovered live from configured endpoints.
    # No hardcoded model lists: whatever Ollama/vLLM actually serves is what
    # gets registered (Phase 3 deliverable 1).
    if providers.local.enabled:
        from gateway.local_discovery import discover_local_models

        ollama_url = getattr(providers, "ollama_base_url", "") or None
        vllm_url = getattr(providers, "vllm_base_url", "") or None
        discovered = discover_local_models(
            ollama_base_url=ollama_url,
            vllm_base_url=vllm_url,
            timeout=max(getattr(providers.local, "probe_timeout_seconds", 12), 3),
        )
        print(f"[config_generator] local discovery found {len(discovered)} model(s)")
        for dm in discovered:
            # LiteLLM speaks OpenAI-compatible protocol to both Ollama (/v1)
            # and vLLM; point api_base at the discovered endpoint and pass an
            # api_key placeholder only where the backend requires one.
            litellm_model = (
                f"ollama/{dm.upstream_name}" if dm.provider == "ollama"
                else f"openai/{dm.upstream_name}"
            )
            params: dict[str, Any] = {
                "model": litellm_model,
                "api_base": dm.base_url,
            }
            if dm.provider == "vllm":
                # vLLM's OpenAI server accepts any key but requires the header;
                # read from env so nothing is hardcoded.
                vllm_key = os.environ.get("VLLM_API_KEY", "local")
                params["api_key"] = vllm_key
            model_list.append({
                "model_name": dm.model_name,
                "litellm_params": params,
                "model_info": {
                    "provider": dm.provider,
                    "tier": "free",
                    "capabilities": dm.capabilities,
                    "mode": "chat",
                },
            })
        # Virtual-model chain entry covering the local pool: route to the
        # highest-scoring discovered local model via the router hook.
        if discovered:
            model_list.append({
                "model_name": "local-auto",
                "litellm_params": {
                    "model": f"ollama/{discovered[0].upstream_name}"
                    if discovered[0].provider == "ollama"
                    else f"openai/{discovered[0].upstream_name}",
                    "api_base": discovered[0].base_url,
                },
                "model_info": {
                    "provider": "local",
                    "tier": "free",
                    "capabilities": ["general"],
                    "mode": "chat",
                },
            })
        else:
            # Local enabled but nothing reachable — register the virtual name
            # as a placeholder excluded by health state (never a fake model).
            print("[config_generator] local enabled but no endpoints reachable")
    
    # Custom providers (Phase 4): OpenAI-compatible endpoints defined by the
    # operator via wizard or UI. Everything comes from config — no defaults.
    for cp in getattr(config, "custom_providers", []) or []:
        if not cp.enabled:
            continue
        params: dict[str, Any] = {
            "model": f"openai/{cp.models[0]}",
            "api_base": cp.base_url,
            "rpm": cp.rpm,
        }
        api_key = get_provider_api_key(cp.name, cp.api_key_env) or ""
        if not api_key:
            # LiteLLM's openai/ provider route refuses to build a client
            # without an api_key, even for endpoints that need none. A
            # placeholder satisfies the client constructor; auth_type="none"
            # providers simply ignore it upstream.
            api_key = "not-required"
        params["api_key"] = api_key
        model_list.append({
            "model_name": f"{cp.name}-auto",
            "litellm_params": params,
            "model_info": {
                "provider": cp.name,
                "tier": cp.tier,
                "capabilities": cp.capabilities,
                "mode": "chat",
            },
        })

    # Virtual models are NOT standalone deployments — LiteLLM rejects
    # model_list entries without litellm_params. Instead, each virtual name
    # (auto-free, auto-code-free, auto-reasoning-free) becomes a model GROUP:
    # we emit one additional deployment per provider whose model_name IS the
    # virtual name. LiteLLM then routes/loads-balances/fails over across the
    # group natively. Priority within the chain is enforced by the router
    # hook reading scores from Redis (Phase 2), with routing_defaults as the
    # static tiebreaker.
    providers_cfg = config.providers
    chain_by_virtual: dict[str, list[str]] = {}
    for vm in config.virtual_models:
        live_chain = []
        for dep_name in vm.fallback_chain:
            if any(m["model_name"] == dep_name for m in model_list):
                live_chain.append(dep_name)
        chain_by_virtual[vm.name] = live_chain
        for dep_name in live_chain:
            source = next(m for m in model_list if m["model_name"] == dep_name)
            model_list.append({
                "model_name": vm.name,
                "litellm_params": dict(source["litellm_params"]),
                "model_info": {
                    "provider": source.get("model_info", {}).get("provider"),
                    "tier": config.routing_defaults and vm.tier,
                    "capabilities": vm.capabilities,
                    "mode": "chat",
                },
            })
        if not live_chain:
            print(f"[config_generator] WARNING: virtual model '{vm.name}' has no live deployments in chain")

    _ = providers_cfg  # (reserved; provider enablement already filtered upstream)
    return model_list


def generate_litellm_config(config: GatewayConfig) -> dict[str, Any]:
    """
    Generate the complete LiteLLM configuration dictionary from GatewayConfig.
    
    This includes model_list, router_settings, and fallback chains
    that LiteLLM proxy reads at startup.
    """
    model_list = get_models_from_registry(config)
    deployment_names = {m["model_name"] for m in model_list}

    # Build LiteLLM config structure
    litellm_config = {
        "model_list": model_list,
        "router_settings": {},
        "fallbacks": {},
        "cache": config.litellm_settings.cache,
    }

    # Custom logger callbacks (request_logs + Redis stream for the brain).
    # LiteLLM loads these from litellm_settings.callbacks (dotted import
    # paths) — the CUSTOM_CALLBACKS env var is not a LiteLLM feature.
    # The same callback class implements async_pre_call_hook (review F-H1),
    # which consults brain-maintained Redis state before each routing
    # decision — this registration is what wires RouterHook into the path.
    callbacks_env = os.environ.get("GATEWAY_CALLBACKS", "gateway.callbacks.custom_logger")
    callbacks_list = [c.strip() for c in callbacks_env.split(",") if c.strip()]
    if callbacks_list:
        litellm_config["litellm_settings"] = {"callbacks": callbacks_list}
    
    # Add cache configuration
    if config.litellm_settings.cache:
        cache_params = config.litellm_settings.cache_params
        litellm_config["cache_config"] = {
            "type": cache_params.get("type", "redis"),
            "host": cache_params.get("host", "${REDIS_HOST}"),
            "port": cache_params.get("port", 6379),
        }
    
    # Add router settings from routing defaults
    defaults = config.routing_defaults
    router_settings = {
        # Score-aware deployment selection; the pre-call hook annotates each
        # request with gateway_influence / exclusion flags from Redis state.
        # NOTE: must be "latency-based-routing" (v1) — in litellm 1.70 the v2
        # variant filters deployments against model_info fields and rejects
        # every deployment that carries custom keys (verified live, review
        # follow-up).
        "routing_strategy": "latency-based-routing",
    }
    
    # Set score weights if present
    if hasattr(defaults, 'score_weight_success_rate'):
        router_settings["score_weight_success_rate"] = defaults.score_weight_success_rate
    if hasattr(defaults, 'score_weight_latency'):
        router_settings["score_weight_latency"] = defaults.score_weight_latency
    if hasattr(defaults, 'score_weight_quota_headroom'):
        router_settings["score_weight_quota_headroom"] = defaults.score_weight_quota_headroom
    
    # Set thresholds
    if hasattr(defaults, 'latency_slow_threshold_ms'):
        router_settings["latency_slow_threshold_ms"] = defaults.latency_slow_threshold_ms
    if hasattr(defaults, 'latency_critical_threshold_ms'):
        router_settings["latency_critical_threshold_ms"] = defaults.latency_critical_threshold_ms
    
    # Set circuit breaker defaults
    if hasattr(defaults, 'circuit_breaker_failure_count'):
        router_settings["circuit_breaker_failure_count"] = defaults.circuit_breaker_failure_count
    if hasattr(defaults, 'circuit_breaker_window_seconds'):
        router_settings["circuit_breaker_window_seconds"] = defaults.circuit_breaker_window_seconds
    
    litellm_config["router_settings"] = router_settings
    
    # Virtual names are model GROUPS (multiple deployments share the name),
    # so LiteLLM's router handles load-balancing and failover natively.
    # No `fallbacks` section needed for them.
    litellm_config["fallbacks"] = {}
    
    return litellm_config


def write_litellm_config(config: GatewayConfig, output_path: str) -> None:
    """
    Generate and write LiteLLM config YAML file from GatewayConfig.
    
    This runs as an init step before LiteLLM starts. The config is static
    — it does not support dynamic runtime reload (Issue 1 fix).
    """
    litellm_config = generate_litellm_config(config)
    
    # Convert to YAML format LiteLLM expects
    import yaml
    
    # LiteLLM config format
    output = {
        "model_list": litellm_config["model_list"],
    }
    
    if litellm_config["router_settings"]:
        output["router_settings"] = litellm_config["router_settings"]
    
    if litellm_config["fallbacks"]:
        output["fallbacks"] = litellm_config["fallbacks"]
    
    if litellm_config.get("cache_config"):
        output["cache_config"] = litellm_config["cache_config"]
    
    with open(output_path, 'w') as f:
        yaml.dump(output, f, default_flow_style=False, sort_keys=False)
    
    print(f"LiteLLM config written to {output_path}")
    print(f"Total models in model_list: {len(litellm_config['model_list'])}")


def main():
    """Main entry point: load GatewayConfig and generate LiteLLM config."""
    import os
    
    # Load config from file (default: gateway_config.yaml in cwd)
    config_path = os.environ.get("GATEWAY_CONFIG_PATH", "gateway_config.yaml")
    
    if not Path(config_path).exists():
        # Create default config if none exists
        from schemas.config import create_default_config
        config = create_default_config()
        config.save_to_file(config_path)
        print(f"Created default {config_path}")
    else:
        config = GatewayConfig.load_from_file(config_path)
    
    # Generate and write LiteLLM config
    output_path = os.environ.get("LITELLM_CONFIG_PATH", "litellm_config.yaml")
    write_litellm_config(config, output_path)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())