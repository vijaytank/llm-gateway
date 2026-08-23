"""
gateway/local_discovery.py — Auto-discovery of local models (Ollama, vLLM).

Per Phase 3 deliverable 1: on startup, if providers.local.enabled=true,
probe the configured local endpoints and register available models.

- Ollama: GET {ollama_base_url}/api/tags → {"models": [{"name": "llama3:8b", ...}]}
- vLLM:   GET {vllm_base_url}/v1/models → OpenAI models list

Discovered models become ModelRegistry rows (provider=ollama/vllm,
tier=free, source=builtin) with capabilities classified from the model
name/family — no hardcoded model lists. They appear LAST in fallback chains.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OLLAMA_URL = "http://localhost:11434"


@dataclass
class DiscoveredModel:
    provider: str            # "ollama" or "vllm"
    model_name: str          # LiteLLM-facing name, e.g. "local-llama3-8b"
    upstream_name: str       # Name the backend expects, e.g. "llama3:8b"
    base_url: str
    capabilities: List[str] = field(default_factory=lambda: ["general"])
    context_window: Optional[int] = None


# ---------------------------------------------------------------------------
# Capability classification (heuristic on model family/size — no hardcoded lists)
# ---------------------------------------------------------------------------

_CODE_HINTS = ("cod", "coder", "codegemma", "starcoder", "deepseek-coder", "qwen2.5-coder")
_REASONING_HINTS = ("r1", "think", "reason", "qwq")


def classify_capabilities(model_id: str) -> List[str]:
    """Classify a discovered model's capabilities from its identifier."""
    lower = model_id.lower()
    caps = []
    if any(h in lower for h in _CODE_HINTS):
        caps.append("code")
    if any(h in lower for h in _REASONING_HINTS):
        caps.append("reasoning")
    # Every model can do general chat; code/reasoning-capable models too.
    caps.append("general")
    return caps


def sanitize_model_name(model_id: str) -> str:
    """Turn an upstream id like 'llama3:8b-instruct-q4' into 'llama3-8b-instruct-q4'
    prefixed for the local pool: 'local-...'."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", model_id).strip("-").lower()
    return f"local-{cleaned}"


# ---------------------------------------------------------------------------
# Endpoint probing
# ---------------------------------------------------------------------------

def probe_ollama(base_url: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Return raw model entries from Ollama's /api/tags. Raises on failure."""
    url = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{url}/api/tags")
        resp.raise_for_status()
        data = resp.json()
    models = data.get("models") or []
    out = []
    for m in models:
        name = m.get("name") or m.get("model")
        if name:
            out.append({
                "id": name,
                "size_bytes": m.get("size"),
                "details": m.get("details") or {},
            })
    return out


def probe_vllm(base_url: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
    """Return raw model entries from a vLLM OpenAI-compatible /v1/models."""
    url = base_url.rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{url}/v1/models")
        resp.raise_for_status()
        data = resp.json()
    out = []
    for m in data.get("data") or []:
        mid = m.get("id")
        if mid:
            ctx = (m.get("max_model_len") if isinstance(m, dict) else None)
            out.append({"id": mid, "context_window": ctx})
    return out


def discover_local_models(
    ollama_base_url: Optional[str] = None,
    vllm_base_url: Optional[str] = None,
    timeout: float = 5.0,
) -> List[DiscoveredModel]:
    """
    Probe both configured local endpoints. A failing/unreachable endpoint is
    skipped (logged), not fatal — local is optional by design.
    """
    ollama_url = ollama_base_url or os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)
    vllm_url = vllm_base_url or os.environ.get("VLLM_BASE_URL", "")
    discovered: List[DiscoveredModel] = []

    if ollama_url:
        try:
            for entry in probe_ollama(ollama_url, timeout=timeout):
                discovered.append(DiscoveredModel(
                    provider="ollama",
                    model_name=sanitize_model_name(entry["id"]),
                    upstream_name=entry["id"],
                    base_url=ollama_url.rstrip("/"),
                    capabilities=classify_capabilities(entry["id"]),
                ))
        except Exception as e:
            print(f"[local_discovery] Ollama at {ollama_url} unreachable: {e}")

    if vllm_url:
        try:
            for entry in probe_vllm(vllm_url, timeout=timeout):
                discovered.append(DiscoveredModel(
                    provider="vllm",
                    model_name=sanitize_model_name(entry["id"]),
                    upstream_name=entry["id"],
                    base_url=vllm_url.rstrip("/"),
                    capabilities=classify_capabilities(entry["id"]),
                    context_window=entry.get("context_window"),
                ))
        except Exception as e:
            print(f"[local_discovery] vLLM at {vllm_url} unreachable: {e}")

    return discovered


# ---------------------------------------------------------------------------
# Registry persistence
# ---------------------------------------------------------------------------

def register_discovered_models(discovered: List[DiscoveredModel],
                               database_url: str) -> int:
    """
    Upsert discovered models into model_registry. Returns count written.
    Uses the same dialect-portable session factory as the rest of the gateway.
    """
    if not discovered:
        return 0
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from schemas.db import ModelRegistry  # local import: db module owns the models

    engine = create_engine(database_url)
    session = Session(engine)
    written = 0
    try:
        for dm in discovered:
            existing = (
                session.query(ModelRegistry)
                .filter(ModelRegistry.provider == dm.provider,
                        ModelRegistry.model_name == dm.upstream_name)
                .one_or_none()
            )
            if existing:
                existing.enabled = True
                existing.capabilities = dm.capabilities
                extra = dict(existing.extra or {})
                extra.update({
                    "litellm_name": dm.model_name,
                    "base_url": dm.base_url,
                })
                existing.extra = extra
            else:
                session.add(ModelRegistry(
                    provider=dm.provider,
                    model_name=dm.upstream_name,
                    tier="free",
                    capabilities=dm.capabilities,
                    enabled=True,
                    probe_timeout_seconds=12,
                    source="builtin",
                    extra={
                        "litellm_name": dm.model_name,
                        "base_url": dm.base_url,
                        "discovered_by": "local_discovery",
                    },
                ))
            written += 1
        session.commit()
    finally:
        session.close()
        engine.dispose()
    return written


def run_discovery(config) -> List[DiscoveredModel]:
    """
    Full Phase 3 discovery pass driven by GatewayConfig.providers.local.
    Returns discovered models; registers them when a database URL is available.
    """
    local_cfg = config.providers.local
    if not getattr(local_cfg, "enabled", False):
        print("[local_discovery] local.enabled=false — skipping discovery")
        return []

    # ProvidersConfig carries the local URLs as sibling fields.
    providers = config.providers
    ollama_url = getattr(providers, "ollama_base_url", "") or None
    vllm_url = getattr(providers, "vllm_base_url", "") or None

    discovered = discover_local_models(
        ollama_base_url=ollama_url,
        vllm_base_url=vllm_url,
        timeout=max(getattr(local_cfg, "probe_timeout_seconds", 12), 3),
    )
    print(f"[local_discovery] found {len(discovered)} local model(s)")

    db_url_env = os.environ.get("GATEWAY_DB_URL", "")
    try:
        db_url = config.general_settings.database_url if config.general_settings else ""
    except Exception:
        db_url = ""
    effective_db = db_url if db_url and not db_url.startswith("${") else db_url_env
    if effective_db and not effective_db.startswith("${"):
        try:
            n = register_discovered_models(discovered, effective_db)
            print(f"[local_discovery] registered {n} model(s) in registry")
        except Exception as e:
            print(f"[local_discovery] registry write skipped: {e}")
    else:
        print("[local_discovery] no database URL — discovery results not persisted")

    return discovered
