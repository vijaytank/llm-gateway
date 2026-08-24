"""
scripts/seed_model_registry.py — Seeds built-in free models into model_registry

Run by docker db-init (docker-compose one-shot) and by the wizard.
Idempotent: upserts on (provider, model_name).

Data source: llm-rate-limits-tracker + nvidia-free-endpoints (external repos,
Phase 0 audit). Quotas reflect Issue 8 fix: OpenRouter effective RPD=40.

Usage:
    python scripts/seed_model_registry.py
"""

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from schemas.db import ModelRegistry

# Built-in free models. capabilities follow the virtual-model chain needs:
# general / code / reasoning
BUILTIN_MODELS = [
    # ---- NVIDIA NIM (free tier) ----
    {"provider": "nvidia", "model_name": "meta/llama-3.1-8b-instruct",
     "tier": "free", "capabilities": ["general"], "rpm": 40, "rpd": 100000},
    {"provider": "nvidia", "model_name": "meta/llama-3.3-70b-instruct",
     "tier": "free", "capabilities": ["general", "reasoning"], "rpm": 40, "rpd": 100000},
    {"provider": "nvidia", "model_name": "qwen/qwen2.5-coder-32b-instruct",
     "tier": "free", "capabilities": ["code"], "rpm": 40, "rpd": 100000},
    {"provider": "nvidia", "model_name": "deepseek-ai/deepseek-r1",
     "tier": "free", "capabilities": ["reasoning"], "rpm": 40, "rpd": 100000},
    # ---- Groq (free tier) ----
    {"provider": "groq", "model_name": "llama-3.1-8b-instant",
     "tier": "free", "capabilities": ["general"], "rpm": 30, "rpd": 14400},
    {"provider": "groq", "model_name": "llama-3.3-70b-versatile",
     "tier": "free", "capabilities": ["general", "reasoning"], "rpm": 30, "rpd": 14400},
    {"provider": "groq", "model_name": "qwen-2.5-coder-32b",
     "tier": "free", "capabilities": ["code"], "rpm": 30, "rpd": 14400},
    # ---- Cerebras (free tier) ----
    {"provider": "cerebras", "model_name": "llama3.1-8b",
     "tier": "free", "capabilities": ["general"], "rpm": 30, "rpd": 1000000},
    {"provider": "cerebras", "model_name": "qwen-2.5-32b",
     "tier": "free", "capabilities": ["general", "reasoning"], "rpm": 30, "rpd": 1000000},
    # ---- OpenRouter (last resort, Issue 8) ----
    {"provider": "openrouter", "model_name": "meta-llama/llama-3.1-8b-instruct:free",
     "tier": "free", "capabilities": ["general"], "rpm": 20,
     "rpd": 40, "warn_on_charge_risk": True},  # effective RPD=40 per Issue 8
    {"provider": "openrouter", "model_name": "qwen/qwen-2.5-coder-32b-instruct:free",
     "tier": "free", "capabilities": ["code"], "rpm": 20,
     "rpd": 40, "warn_on_charge_risk": True},
]


def seed_model_registry(database_url: str = None) -> dict:
    """Upsert built-in models into model_registry. Returns a status dict."""
    url = (database_url or os.environ.get("GATEWAY_DB_URL")
           or os.environ.get("DATABASE_URL"))
    if not url:
        return {"status": "error", "message": "DATABASE_URL not set"}

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(url)
    seeded = 0
    updated = 0

    with Session(engine) as session:
        for spec in BUILTIN_MODELS:
            existing = (
                session.query(ModelRegistry)
                .filter_by(provider=spec["provider"], model_name=spec["model_name"])
                .one_or_none()
            )
            if existing is None:
                extra = {}
                if spec.get("warn_on_charge_risk"):
                    # Persisted in the JSONB `extra` column (review F-M6):
                    # feeds UI charge-risk warnings for OpenRouter free tiers.
                    extra["warn_on_charge_risk"] = True
                session.add(ModelRegistry(
                    id=uuid.uuid4(),
                    provider=spec["provider"],
                    model_name=spec["model_name"],
                    tier=spec.get("tier", "free"),
                    capabilities=spec.get("capabilities", []),
                    enabled=True,
                    probe_timeout_seconds=spec.get("probe_timeout_seconds", 12),
                    rpm=spec.get("rpm"),
                    rpd=spec.get("rpd"),
                    source="builtin",
                    extra=extra,
                ))
                seeded += 1
            else:
                # Refresh quotas/capabilities but preserve user edits to enabled
                existing.capabilities = spec.get("capabilities", existing.capabilities)
                existing.rpm = spec.get("rpm", existing.rpm)
                existing.rpd = spec.get("rpd", existing.rpd)
                if spec.get("warn_on_charge_risk"):
                    extra = dict(existing.extra or {})
                    extra["warn_on_charge_risk"] = True
                    existing.extra = extra
                updated += 1
        session.commit()

    return {
        "status": "success",
        "models_seeded": seeded,
        "models_updated": updated,
        "total_builtin": len(BUILTIN_MODELS),
    }


if __name__ == "__main__":
    result = seed_model_registry()
    print(result)
    sys.exit(0 if result["status"] == "success" else 1)
