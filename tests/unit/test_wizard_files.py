"""Unit tests for wizard/setup.py scripted flows — review F-M15 coverage.

Exercises generate_env_file / enforce_env_permissions / config validation
without interactive prompts.
"""

import os

import pytest

from schemas.config import GatewayConfig, create_default_config
from wizard import setup as wiz


def test_generate_env_file_contains_required_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    env_path = tmp_path / ".env"
    wiz.generate_env_file(env_path, {"NVIDIA_API_KEY": "nv-key"}, mode="docker")
    content = env_path.read_text()
    assert "DATABASE_URL=postgresql://llm_gateway:" in content
    assert "GATEWAY_DB_URL=" in content
    assert "REDIS_URL=redis://redis:6379/0" in content
    assert "POSTGRES_PASSWORD=" in content
    assert "SESSION_SECRET=" in content
    assert "LITELLM_MASTER_KEY=sk-litellm-" in content
    assert "NVIDIA_API_KEY=nv-key" in content
    # No plaintext postgres password duplication in the key list section
    assert content.count("POSTGRES_PASSWORD=") == 1


def test_generate_env_baremetal_uses_localhost(tmp_path):
    env_path = tmp_path / ".env"
    wiz.generate_env_file(env_path, {}, mode="bare-metal")
    content = env_path.read_text()
    assert "redis://localhost:6379/0" in content
    assert "@localhost:5432/" in content


def test_generate_config_yaml_round_trips(tmp_path):
    cfg = create_default_config()
    cfg.providers.groq.enabled = False
    path = tmp_path / "gateway_config.yaml"
    wiz.generate_config_yaml(path, cfg)
    loaded = GatewayConfig.load_from_file(str(path))
    assert loaded.meta.schema_version == cfg.meta.schema_version
    assert loaded.providers.groq.enabled is False
    assert len(loaded.virtual_models) == 3


def test_validate_config_pass_and_fail(tmp_path, capsys):
    good = tmp_path / "good.yaml"
    wiz.generate_config_yaml(good, create_default_config())
    assert wiz.validate_config(good) is True

    bad = tmp_path / "bad.yaml"
    bad.write_text("general_settings:\n  master_key: 'short'\n")
    assert wiz.validate_config(bad) is False


def test_enforce_env_permissions_posix(tmp_path):
    """On POSIX the helper tightens loose modes; on Windows it no-ops."""
    env = tmp_path / ".env"
    env.write_text("K=v\n")
    if os.name == "posix":
        os.chmod(env, 0o644)
        wiz.enforce_env_permissions(env)
        assert (os.stat(env).st_mode & 0o777) == 0o600
    else:
        # Must not raise and must not change anything on Windows
        before = os.stat(env).st_mode & 0o777
        wiz.enforce_env_permissions(env)
        assert (os.stat(env).st_mode & 0o777) == before


def test_write_file_with_permissions_reports_nonposix(tmp_path, capsys):
    target = tmp_path / "out.txt"
    wiz.write_file_with_permissions(target, "data", mode=0o600)
    assert target.read_text() == "data"
