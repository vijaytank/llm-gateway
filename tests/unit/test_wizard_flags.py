"""
tests/unit/test_wizard_flags.py — Phase 2 tests for wizard decoupling and CLI flags.

Covers:
  - --docker flag bypasses prompt and sets deploy_mode to "docker"
  - In docker mode, run_db_init is skipped
  - --regenerate-config headlessly creates valid .env and config
  - --non-interactive runs headlessly
  - _read_password fallback behavior
"""

import os
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import wizard.setup as setup_mod
from schemas.config import GatewayConfig


def test_wizard_docker_flag_skips_db_init(tmp_path, monkeypatch):
    """--docker flag sets docker mode and skips host run_db_init."""
    monkeypatch.chdir(tmp_path)
    
    db_init_called = []
    monkeypatch.setattr(setup_mod, "run_db_init", lambda: db_init_called.append(True))
    
    rc = setup_mod.main(["--docker", "--scripted", "--force"])
    assert rc == 0
    assert len(db_init_called) == 0, "run_db_init should NOT be called in docker mode"
    
    env_content = (tmp_path / ".env").read_text()
    assert "@postgres:5432" in env_content
    assert "SECRET_ENCRYPTION_KEY=" in env_content
    assert (tmp_path / "gateway_config.yaml").exists()


def test_wizard_regenerate_config_headless(tmp_path, monkeypatch):
    """--regenerate-config generates valid .env and config without stdin prompts."""
    monkeypatch.chdir(tmp_path)
    
    # Pre-populate some keys in .env
    (tmp_path / ".env").write_text(
        "GROQ_API_KEY=gsk_existing_123\n"
        "POSTGRES_PASSWORD=custom_pg_pw_456\n"
    )
    
    rc = setup_mod.main(["--regenerate-config"])
    assert rc == 0
    
    new_env = (tmp_path / ".env").read_text()
    assert "GROQ_API_KEY=gsk_existing_123" in new_env
    assert "POSTGRES_PASSWORD=custom_pg_pw_456" in new_env
    assert "SECRET_ENCRYPTION_KEY=" in new_env
    
    config = GatewayConfig.load_from_file(str(tmp_path / "gateway_config.yaml"))
    assert config.meta.schema_version == "1.0"


def test_wizard_non_interactive_flag(tmp_path, monkeypatch):
    """--non-interactive runs headlessly without prompts."""
    monkeypatch.chdir(tmp_path)
    
    rc = setup_mod.main(["--non-interactive", "--force"])
    assert rc == 0
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "gateway_config.yaml").exists()


def test_read_password_fallback(monkeypatch):
    """_read_password falls back to getpass and handles EOF gracefully."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "test_secret_123")
    pw = setup_mod._read_password("Password: ")
    assert pw == "test_secret_123"

    # Non-interactive stdin fallback
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin_password_999\n"))
    pw_stdin = setup_mod._read_password("Password: ")
    assert pw_stdin == "stdin_password_999"


def test_choose_ui_password_skip_in_docker(monkeypatch, capsys):
    """In docker mode or on empty input, choose_ui_password skips gracefully."""
    monkeypatch.setattr(setup_mod, "_read_password", lambda *a, **k: "")
    setup_mod.choose_ui_password(is_docker=True)
    captured = capsys.readouterr().out
    assert "Skipped" in captured
