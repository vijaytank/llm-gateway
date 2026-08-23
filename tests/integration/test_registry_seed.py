"""
test_registry_seed.py — Plan Phase 0/1: model registry seeding.

Covers:
  - Seeded builtins match scripts/seed_model_registry.BUILTIN_MODELS exactly
    (read from the repo, not hardcoded in the test)
  - Seeding is idempotent: db-init re-run produces no duplicates
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # project root
from conftest import compose_output, pg_query  # noqa: E402


def _registry_rows():
    return pg_query(
        "SELECT provider, model_name, tier, enabled FROM model_registry "
        "ORDER BY provider, model_name"
    )


def test_builtin_models_seeded():
    from scripts.seed_model_registry import BUILTIN_MODELS

    rows = _registry_rows()
    seeded = {(r[0], r[1]) for r in rows}
    for spec in BUILTIN_MODELS:
        key = (spec["provider"], spec["model_name"])
        assert key in seeded, f"builtin {key} missing from registry"


def test_seeded_models_enabled_and_tiered():
    from scripts.seed_model_registry import BUILTIN_MODELS

    rows = _registry_rows()
    by_key = {(r[0], r[1]): r for r in rows}
    for spec in BUILTIN_MODELS:
        key = (spec["provider"], spec["model_name"])
        assert key in by_key, f"{key} not seeded"
        _, _, tier, enabled = by_key[key]
        assert tier == spec["tier"]
        assert enabled is True or enabled == "t"


def test_seed_is_idempotent():
    """Re-running the seeder (as db-init does on every deploy) never dupes."""
    before = len(_registry_rows())
    out = compose_output(
        "exec", "-T", "gateway",
        "python", "-c",
        "import sys; sys.path.insert(0, '/app'); "
        "from scripts.seed_model_registry import seed_model_registry; "
        "import os; print(seed_model_registry(os.environ['DATABASE_URL'].replace('***', __import__('os').environ.get('PGPASSWORD_FROM_ENV',''))))",
        timeout=120,
    )
    # Run via the gateway container where DATABASE_URL is already correct.
    # The exec above may fail on env plumbing; fall back to direct re-exec of
    # the seed script inside the container.
    if "status" not in out:
        out = compose_output(
            "exec", "-T", "gateway",
            "bash", "-c",
            "cd /app && DATABASE_URL=$DATABASE_URL python -c \""
            "import os; from scripts.seed_model_registry import seed_model_registry; "
            "print(seed_model_registry())\"",
            timeout=120,
        )
    assert '"status"' in out and ("seeded" in out or "updated" in out), \
        f"unexpected seeder output: {out[-500:]}"
    after = len(_registry_rows())
    assert after == before, f"re-seed changed row count {before} -> {after} (not idempotent)"
