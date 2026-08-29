"""tests/unit/test_ui_security.py — P1.3 Security Settings endpoint tests.

Covers the security rotation endpoints:
- /security page rendering
- /security/rotate-admin-password
- /security/rotate-master-key
- /security/rotate-session-secret
- /security/rotate-encryption-key
- require_admin enforcement on /credentials routes
"""

import os
import sys
import urllib.parse
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from schemas.db import Base, ProviderCredential, UiSetting, encrypt_api_key


@pytest.fixture()
def ui_env(tmp_path, monkeypatch):
    """Isolated DB + config for the security routes."""
    db_path = tmp_path / "ui_security.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GATEWAY_DB_URL", f"sqlite:///{db_path}")
    config_path = tmp_path / "gateway_config.yaml"
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-signed-cookies-0123456789")

    import ui.app as app_module
    monkeypatch.setattr(app_module, "_engine", None)
    monkeypatch.setattr(app_module, "_SessionLocal", None)

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    from schemas.config import create_default_config
    create_default_config().save_to_file(str(config_path))

    import fakeredis
    fake_r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(app_module, "get_redis", lambda: fake_r)

    yield app_module


@pytest.fixture()
def client(ui_env):
    return TestClient(ui_env.app)


def _login(client):
    client.post("/setup", data={"password": "supersecret9", "confirm": "supersecret9"})
    resp = client.post("/login", data={"password": "supersecret9"}, follow_redirects=True)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /security page
# ---------------------------------------------------------------------------

def test_security_page_requires_login(client):
    """Unauthenticated users are redirected to /login."""
    resp = client.get("/security", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] in ("/login", "/setup")


def test_security_page_renders_when_logged_in(client, ui_env):
    """Authenticated admin sees the security page."""
    _login(client)
    resp = client.get("/security")
    assert resp.status_code == 200
    assert "Security Settings" in resp.text
    assert "Admin Password" in resp.text
    assert "LiteLLM Master Key" in resp.text
    assert "Session Secret" in resp.text
    assert "Encryption Key" in resp.text


def test_security_page_shows_configured_status(client, ui_env, tmp_path):
    """Page shows which credentials are configured."""
    _login(client)
    # Set up a .env file with some keys
    env_path = tmp_path / ".env"
    env_path.write_text("LITELLM_MASTER_KEY=sk-litellm-test123456\nSESSION_SECRET=secret123\n")
    # Override ROOT for the test
    import ui.app as app_module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "ROOT", tmp_path)
        resp = client.get("/security")
        assert resp.status_code == 200
        assert "configured" in resp.text


# ---------------------------------------------------------------------------
# /security/rotate-admin-password
# ---------------------------------------------------------------------------

def test_rotate_admin_password_success(client, ui_env):
    """Admin password can be changed with correct current password."""
    _login(client)
    resp = client.post("/security/rotate-admin-password", data={
        "current_password": "supersecret9",
        "new_password": "newpassword1",
        "confirm_password": "newpassword1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "Admin+password+updated" in resp.headers["location"]

    # Verify old password no longer works
    resp = client.post("/login", data={"password": "supersecret9"}, follow_redirects=False)
    assert resp.status_code == 401

    # Verify new password works
    resp = client.post("/login", data={"password": "newpassword1"}, follow_redirects=False)
    assert resp.status_code == 303


def test_rotate_admin_password_wrong_current(client, ui_env):
    """Rotation fails if current password is wrong."""
    _login(client)
    resp = client.post("/security/rotate-admin-password", data={
        "current_password": "wrongpassword",
        "new_password": "newpassword1",
        "confirm_password": "newpassword1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "Current+password+is+incorrect" in resp.headers["location"]


def test_rotate_admin_password_mismatch(client, ui_env):
    """Rotation fails if new passwords don't match."""
    _login(client)
    resp = client.post("/security/rotate-admin-password", data={
        "current_password": "supersecret9",
        "new_password": "newpassword1",
        "confirm_password": "differentpassword",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "New+passwords+do+not+match" in resp.headers["location"]


def test_rotate_admin_password_too_short(client, ui_env):
    """Rotation fails if new password is too short."""
    _login(client)
    resp = client.post("/security/rotate-admin-password", data={
        "current_password": "supersecret9",
        "new_password": "short",
        "confirm_password": "short",
    }, follow_redirects=False)
    assert resp.status_code == 303
    decoded_loc = urllib.parse.unquote_plus(resp.headers["location"])
    assert "at least 8 characters" in decoded_loc


# ---------------------------------------------------------------------------
# /security/rotate-master-key
# ---------------------------------------------------------------------------

def test_rotate_master_key_success(client, ui_env, tmp_path):
    """Master key can be rotated and written to .env."""
    _login(client)
    env_path = tmp_path / ".env"
    env_path.write_text("LITELLM_MASTER_KEY=sk-litellm-old\n")

    import ui.app as app_module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "ROOT", tmp_path)
        resp = client.post("/security/rotate-master-key", data={
            "new_master_key": "sk-litellm-newkey123456",
            "confirm": "sk-litellm-newkey123456",
        }, follow_redirects=False)
        assert resp.status_code == 303
        assert "Master+key+updated" in resp.headers["location"]

        # Verify .env was updated
        env_content = env_path.read_text()
        assert "sk-litellm-newkey123456" in env_content


def test_rotate_master_key_mismatch(client, ui_env):
    """Rotation fails if keys don't match."""
    _login(client)
    resp = client.post("/security/rotate-master-key", data={
        "new_master_key": "sk-litellm-newkey123456",
        "confirm": "sk-litellm-different",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "Keys+do+not+match" in resp.headers["location"]


def test_rotate_master_key_too_short(client, ui_env):
    """Rotation fails if key is too short."""
    _login(client)
    resp = client.post("/security/rotate-master-key", data={
        "new_master_key": "short",
        "confirm": "short",
    }, follow_redirects=False)
    assert resp.status_code == 303
    decoded_loc = urllib.parse.unquote_plus(resp.headers["location"])
    assert "at least 8 characters" in decoded_loc


# ---------------------------------------------------------------------------
# /security/rotate-session-secret
# ---------------------------------------------------------------------------

def test_rotate_session_secret_success(client, ui_env, tmp_path):
    """Session secret can be rotated and written to .env."""
    _login(client)
    env_path = tmp_path / ".env"
    env_path.write_text("SESSION_SECRET=oldsecret123\n")

    import ui.app as app_module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "ROOT", tmp_path)
        resp = client.post("/security/rotate-session-secret", follow_redirects=False)
        assert resp.status_code == 303
        assert "Session+secret+updated" in resp.headers["location"]

        # Verify .env was updated with a new 64-char hex secret
        env_content = env_path.read_text()
        for line in env_content.splitlines():
            if line.startswith("SESSION_SECRET="):
                new_secret = line.split("=", 1)[1].strip()
                assert len(new_secret) == 64  # 32 bytes hex = 64 chars
                assert new_secret != "oldsecret123"
                break
        else:
            pytest.fail("SESSION_SECRET not found in .env")


# ---------------------------------------------------------------------------
# /security/rotate-encryption-key
# ---------------------------------------------------------------------------

def test_rotate_encryption_key_success(client, ui_env, tmp_path):
    """Encryption key rotation re-encrypts stored credentials."""
    _login(client)

    # Store a credential first
    engine = create_engine(os.environ["GATEWAY_DB_URL"])
    with Session(engine) as s:
        s.add(ProviderCredential(
            provider_name="groq",
            api_key_encrypted=encrypt_api_key("gsk-original-key"),
        ))
        s.commit()

    env_path = tmp_path / ".env"
    env_path.write_text("SECRET_ENCRYPTION_KEY=oldkey123\n")

    import ui.app as app_module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "ROOT", tmp_path)
        resp = client.post("/security/rotate-encryption-key", follow_redirects=False)
        assert resp.status_code == 303
        assert "Encryption+key+rotated" in resp.headers["location"]

        # Verify .env was updated
        env_content = env_path.read_text()
        assert "SECRET_ENCRYPTION_KEY=" in env_content
        assert "oldkey123" not in env_content


# ---------------------------------------------------------------------------
# require_admin on /credentials routes
# ---------------------------------------------------------------------------

def test_require_admin_dependency_raises_403(ui_env):
    """require_admin raises 403 when called directly with an unauthenticated request."""
    from fastapi import Request
    from ui.app import require_admin
    # Create a mock request without session cookie
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/credentials/groq",
        "headers": [],
    }
    req = Request(scope)
    with pytest.raises(HTTPException) as exc_info:
        require_admin(req)
    assert exc_info.value.status_code == 403


def test_credentials_set_requires_admin(client):
    """POST /credentials/{name} requires admin auth (redirect to login or 403)."""
    resp = client.post("/credentials/groq", data={"api_key": "gsk-test"}, follow_redirects=False)
    assert resp.status_code in (303, 403)
    if resp.status_code == 303:
        assert resp.headers["location"] in ("/login", "/setup")


def test_credentials_delete_requires_admin(client):
    """POST /credentials/{name}/delete requires admin auth (redirect to login or 403)."""
    resp = client.post("/credentials/groq/delete", follow_redirects=False)
    assert resp.status_code in (303, 403)
    if resp.status_code == 303:
        assert resp.headers["location"] in ("/login", "/setup")


def test_credentials_set_works_when_admin(client, ui_env):
    """POST /credentials/{name} works when logged in as admin."""
    _login(client)
    resp = client.post("/credentials/groq", data={"api_key": "gsk-test-key"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "API+key+saved" in resp.headers["location"]


def test_credentials_delete_works_when_admin(client, ui_env):
    """POST /credentials/{name}/delete works when logged in as admin."""
    _login(client)
    # Store first
    client.post("/credentials/groq", data={"api_key": "gsk-test-key"})
    # Delete
    resp = client.post("/credentials/groq/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "API+key+removed" in resp.headers["location"]
