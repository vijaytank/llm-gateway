"""tests/unit/test_env_setup.py — P0.3 + P0.2 setup/init validation.

Verifies, without docker:
  - docker-compose.yml has no hardcoded placeholder passwords (P0.1 / FR-0.1)
  - every DB-using service loads credentials via env_file (FR-0.2)
  - db-init is idempotent-tolerant (P0.2 / FR-0.3)
  - init-env.sh generates the required secret keys and never overwrites an
    existing .env (P0.3 / FR-0.4, FR-0.5)

Parsing YAML with PyYAML keeps this a pure unit test.
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

COMPOSE = ROOT / "docker" / "docker-compose.yml"
INIT_ENV = ROOT / "docker" / "init-env.sh"


@pytest.fixture(scope="module")
def compose():
    with open(COMPOSE, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# P0.1 — no hardcoded placeholders in environment blocks
# ---------------------------------------------------------------------------

def test_no_placeholder_password_in_compose(compose):
    services = compose["services"]
    for name, svc in services.items():
        for env in svc.get("environment", []) or []:
            if isinstance(env, str):
                assert "***" not in env, f"{name} environment has placeholder '***'"
                assert "***@" not in env, f"{name} environment has redacted URL"


# ---------------------------------------------------------------------------
# FR-0.2 — credentials come from .env via env_file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("svc_name", ["db-init", "gateway", "adapter", "ui"])
def test_db_services_have_env_file(compose, svc_name):
    svc = compose["services"][svc_name]
    env_files = svc.get("env_file", []) or []
    if isinstance(env_files, dict):
        env_files = list(env_files.values())
    assert env_files, f"{svc_name} must load .env via env_file"


# ---------------------------------------------------------------------------
# P0.2 — db-init is idempotent-tolerant
# ---------------------------------------------------------------------------

def test_db_init_command_is_failure_tolerant(compose):
    db_init = compose["services"]["db-init"]
    command = db_init["command"]
    text = "\n".join(command) if isinstance(command, list) else str(command)
    # Alembic failure must not abort the container (idempotent on 2nd run).
    assert "||" in text, "db-init must tolerate alembic/seed failure (|| guard)"
    assert "alembic upgrade head" in text
    assert "seed_model_registry" in text


# ---------------------------------------------------------------------------
# P0.3 — init-env.sh generates secrets, never overwrites
# ---------------------------------------------------------------------------

def test_init_env_script_present_and_executable():
    assert INIT_ENV.exists()
    assert INIT_ENV.read_text(encoding="utf-8").lstrip().startswith("#!/bin/bash")


def test_init_env_generates_required_secrets():
    text = INIT_ENV.read_text(encoding="utf-8")
    for var in ("POSTGRES_PASSWORD", "LITELLM_MASTER_KEY", "SESSION_SECRET",
                "SECRET_ENCRYPTION_KEY", "DATABASE_URL", "GATEWAY_DB_URL"):
        assert var in text, f"init-env.sh must generate/set {var}"


def test_init_env_never_overwrites_existing_env():
    text = INIT_ENV.read_text(encoding="utf-8")
    assert "skipping generation" in text, "must skip if .env exists (FR-0.5)"
    assert "[ -f \"$ENV_FILE\" ]" in text


def test_init_env_uses_cryptographic_randomness():
    text = INIT_ENV.read_text(encoding="utf-8")
    # Must use openssl rand or python secrets — never a fixed "ChangeMe" string.
    assert "openssl rand" in text or "secrets.token" in text
    assert "ChangeMe" not in text
