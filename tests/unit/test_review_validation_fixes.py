"""
tests/unit/test_review_validation_fixes.py — Tests for the fixes made during
the independent code review validation.

Covers:
- Engine caching in gateway/credentials.py
- Setup endpoint rate limiting in ui/app.py
- Atomic .env updates in ui/app.py
- Per-boot session secret caching in ui/auth.py
"""

import os
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from schemas.db import Base, ProviderCredential, encrypt_api_key
from gateway import credentials as creds
from ui import auth as ui_auth
import ui.app as app_module


def test_credentials_engine_caching(tmp_path, monkeypatch):
    """Verify that _get_session_factory caches the engine instance."""
    db_path = tmp_path / "engine_cache.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("GATEWAY_DB_URL", url)

    # Invalidate and clear
    creds.invalidate_cache()
    factory1 = creds._get_session_factory()
    factory2 = creds._get_session_factory()

    assert factory1 is not None
    assert factory1 is factory2
    assert creds._db_engine is not None


def test_setup_endpoint_rate_limiting(tmp_path, monkeypatch):
    """Verify that /setup POST is rate-limited after 5 consecutive failures."""
    db_path = tmp_path / "setup_rl.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GATEWAY_DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-signed-cookies-0123456789")

    monkeypatch.setattr(app_module, "_engine", None)
    monkeypatch.setattr(app_module, "_SessionLocal", None)
    monkeypatch.setattr(app_module, "_login_limiter", None)

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    import fakeredis
    fake_r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(app_module, "get_redis", lambda: fake_r)

    client = TestClient(app_module.app)

    # 5 failed setup attempts (mismatched password)
    for i in range(5):
        resp = client.post("/setup", data={"password": "validpassword123", "confirm": "mismatch"}, follow_redirects=False)
        assert resp.status_code == 400

    # 6th attempt should be rate-limited (429)
    resp = client.post("/setup", data={"password": "validpassword123", "confirm": "validpassword123"}, follow_redirects=False)
    assert resp.status_code == 429
    assert "Too many attempts" in resp.text


def test_atomic_env_file_update(tmp_path, monkeypatch):
    """Verify that _update_env_file updates keys and does not leave temporary files."""
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nKEY1=old_val\n")
    monkeypatch.setattr(app_module, "ROOT", tmp_path)

    success = app_module._update_env_file("KEY1", "new_val")
    assert success is True
    content = env_file.read_text()
    assert "KEY1=new_val" in content
    assert "FOO=bar" in content

    # Verify no leftover tmp files
    tmp_files = list(tmp_path.glob("*.tmp*"))
    assert len(tmp_files) == 0


def test_per_boot_session_secret_persists(monkeypatch):
    """Verify get_session_secret returns the same bytes across calls during one boot."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setattr(ui_auth, "_per_boot_secret", None)

    secret1 = ui_auth.get_session_secret()
    secret2 = ui_auth.get_session_secret()

    assert secret1 is not None
    assert len(secret1) == 32
    assert secret1 == secret2
