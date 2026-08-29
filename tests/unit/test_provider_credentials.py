"""tests/unit/test_provider_credentials.py — P1.2.x credentials coverage.

Covers the encryption round-trip (P1.2.1), the DB-backed gateway credential
resolution with env fallback + TTL cache (P1.2.3), and the UI credential
management routes (P1.2.2). DB-backed tests use SQLite (dialect-portable
schemas) — no live Postgres.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from schemas.db import (
    Base,
    ProviderCredential,
    decrypt_api_key,
    encrypt_api_key,
    rotate_encryption_key,
)


# ---------------------------------------------------------------------------
# P1.2.1 — model + encryption
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
    assert decrypt_api_key(encrypt_api_key("sk-test-1234")) == "sk-test-1234"


def test_encrypted_value_is_not_plaintext(monkeypatch):
    monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
    enc = encrypt_api_key("nvapi-super-secret")
    assert enc != "nvapi-super-secret"
    assert "nvapi" not in enc


def test_provider_credential_model_stores_encrypted(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
    db_path = tmp_path / "cred.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ProviderCredential(
            provider_name="groq",
            api_key_encrypted=encrypt_api_key("gsk-test-999"),
        ))
        s.commit()
    with Session(engine) as s:
        row = s.get(ProviderCredential, "groq")
        assert row is not None
        # At rest: must be a Fernet token, not the plaintext key.
        assert "gsk-test-999" not in row.api_key_encrypted
        assert decrypt_api_key(row.api_key_encrypted) == "gsk-test-999"


def test_rotate_encryption_key_reencrypts_all(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
    db_path = tmp_path / "cred.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ProviderCredential(provider_name="nvidia",
                                 api_key_encrypted=encrypt_api_key("nvapi-a")))
        s.add(ProviderCredential(provider_name="cerebras",
                                 api_key_encrypted=encrypt_api_key("csk-b")))
        s.commit()

    new_key = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
    with Session(engine) as s:
        rotate_encryption_key(s, new_key)

    with Session(engine) as s:
        for name, plain in (("nvidia", "nvapi-a"), ("cerebras", "csk-b")):
            row = s.get(ProviderCredential, name)
            assert decrypt_api_key(row.api_key_encrypted) == plain


# ---------------------------------------------------------------------------
# P1.2.3 — gateway credential resolution (DB → env fallback, TTL cache)
# ---------------------------------------------------------------------------

def _sqlite_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'gw_cred.db'}"


def test_get_provider_api_key_from_db(tmp_path, monkeypatch):
    from gateway import credentials as creds
    creds.invalidate_cache()
    url = _sqlite_url(tmp_path)
    monkeypatch.setenv("GATEWAY_DB_URL", url)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ProviderCredential(provider_name="groq",
                                 api_key_encrypted=encrypt_api_key("gsk-db-key")))
        s.commit()

    assert creds.get_provider_api_key("groq", "GROQ_API_KEY") == "gsk-db-key"


def test_get_provider_api_key_falls_back_to_env(tmp_path, monkeypatch):
    from gateway import credentials as creds
    creds.invalidate_cache()
    url = _sqlite_url(tmp_path)
    monkeypatch.setenv("GATEWAY_DB_URL", url)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-env-key")
    Base.metadata.create_all(create_engine(url))  # table exists, no row
    assert creds.get_provider_api_key("nvidia", "NVIDIA_API_KEY") == "nvapi-env-key"


def test_get_provider_api_key_none_when_unset(tmp_path, monkeypatch):
    from gateway import credentials as creds
    creds.invalidate_cache()
    monkeypatch.setenv("GATEWAY_DB_URL", _sqlite_url(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    Base.metadata.create_all(create_engine(os.environ["GATEWAY_DB_URL"]))
    assert creds.get_provider_api_key("openrouter", "OPENROUTER_API_KEY") is None


def test_credentials_cache_ttl_observed(tmp_path, monkeypatch):
    """Cache serves the stored value; invalidate_cache drops it so the next
    read re-queries the DB."""
    from gateway import credentials as creds
    creds.invalidate_cache()
    url = _sqlite_url(tmp_path)
    monkeypatch.setenv("GATEWAY_DB_URL", url)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    engine = create_engine(url)
    Base.metadata.create_all(engine)

    with Session(engine) as s:
        s.add(ProviderCredential(provider_name="groq",
                                 api_key_encrypted=encrypt_api_key("gsk-first")))
        s.commit()
    assert creds.get_provider_api_key("groq", "GROQ_API_KEY") == "gsk-first"

    # Update DB row behind the gateway's back, then invalidate — next read
    # must see the new value (proves the cache was actually serving before).
    with Session(engine) as s:
        row = s.get(ProviderCredential, "groq")
        row.api_key_encrypted = encrypt_api_key("gsk-second")
        s.commit()
    # Without invalidate, cache still returns the first value.
    assert creds.get_provider_api_key("groq", "GROQ_API_KEY") == "gsk-first"
    creds.invalidate_cache("groq")
    assert creds.get_provider_api_key("groq", "GROQ_API_KEY") == "gsk-second"


# ---------------------------------------------------------------------------
# P1.2.2 — UI credential management routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def ui_env(tmp_path, monkeypatch):
    """Isolated DB + config for the UI credential routes."""
    db_path = tmp_path / "ui_cred.db"
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
    return app_module


@pytest.fixture()
def client(ui_env):
    from fastapi.testclient import TestClient
    return TestClient(ui_env.app)


def _login(client):
    client.post("/setup", data={"password": "supersecret9", "confirm": "supersecret9"})
    resp = client.post("/login", data={"password": "supersecret9"}, follow_redirects=True)
    assert resp.status_code == 200


def test_credentials_page_lists_builtin_providers(client):
    _login(client)
    html = client.get("/credentials").text
    assert "nvidia" in html and "groq" in html and "cerebras" in html and "openrouter" in html


def test_store_credential_then_masked_display(client):
    _login(client)
    resp = client.post("/credentials/groq", data={"api_key": "gsk-super-secret-key"},
                       follow_redirects=True)
    assert resp.status_code == 200
    html = resp.text
    assert "configured" in html
    # Full key never displayed (FR-1.2.5): only first/last 4 chars.
    assert "gsk-super-secret-key" not in html
    assert "gsk-" in html and "-key" in html


def test_delete_credential(client):
    _login(client)
    client.post("/credentials/groq", data={"api_key": "gsk-secret"})
    resp = client.post("/credentials/groq/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert "not configured" in resp.text


def test_credential_stored_encrypted_in_db(client, ui_env):
    _login(client)
    client.post("/credentials/groq", data={"api_key": "gsk-plaintext-should-not-live-here"})
    from sqlalchemy.orm import Session as DbSession
    from schemas.db import ProviderCredential, decrypt_api_key
    engine = create_engine(os.environ["GATEWAY_DB_URL"])
    with DbSession(engine) as s:
        row = s.get(ProviderCredential, "groq")
        assert row is not None
        assert "gsk-plaintext-should-not-live-here" not in row.api_key_encrypted
        assert decrypt_api_key(row.api_key_encrypted) == "gsk-plaintext-should-not-live-here"
