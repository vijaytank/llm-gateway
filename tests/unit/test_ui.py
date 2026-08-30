"""
tests/unit/test_ui.py — Phase 4 UI tests (plan: test_ui_*.py).

Server-side rendered UI is tested with httpx/TestClient against the real
FastAPI app backed by SQLite (dialect-portable schemas) and fakeredis.
No mock data in production code paths — test fixtures insert real rows.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient


@pytest.fixture()
def ui_env(tmp_path, monkeypatch):
    """Isolated DB + Redis + config for the UI app."""
    db_path = tmp_path / "ui_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("GATEWAY_DB_URL", f"sqlite:///{db_path}")
    config_path = tmp_path / "gateway_config.yaml"
    monkeypatch.setenv("GATEWAY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-signed-cookies-0123456789")

    # Reset module-level engine so each test gets a fresh one
    import ui.app as app_module
    if not hasattr(app_module, "_engine"):
        app_module._engine = None
        app_module._SessionLocal = None
    monkeypatch.setattr(app_module, "_engine", None)
    monkeypatch.setattr(app_module, "_SessionLocal", None)

    # Build schema + seed registry rows
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from schemas.db import Base, ModelRegistry, ModelStatsHourly

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ModelRegistry(provider="nvidia", model_name="meta/llama-3.1-8b-instruct",
                             tier="free", capabilities=["general"]))
        hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        db.add(ModelStatsHourly(model_name="nvidia-auto", provider="nvidia",
                                hour_bucket=hour, request_count=10, error_count=1,
                                avg_latency_ms=420.5))
        db.commit()

    # Default gateway config file on disk
    from schemas.config import create_default_config
    create_default_config().save_to_file(str(config_path))

    # Route UI Redis access to fakeredis so tests never touch a live server
    import fakeredis
    fake_r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(app_module, "get_redis", lambda: fake_r)

    yield app_module


@pytest.fixture()
def client(ui_env):
    return TestClient(ui_env.app)


# ---------------------------------------------------------------------------
# Auth (test_ui_auth.py)
# ---------------------------------------------------------------------------

def test_unauthenticated_redirects_to_login_or_setup(client):
    for path in ("/", "/config", "/stats", "/logs"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        # First run (no admin password) → setup; afterwards → login
        assert resp.headers["location"] in ("/login", "/setup"), path


def test_health_is_public(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_setup_then_login_flow(client):
    # Set password via /setup
    resp = client.post("/setup", data={"password": "supersecret9", "confirm": "supersecret9"},
                       follow_redirects=False)
    assert resp.status_code == 303

    # Wrong password → 401 shape, no cookie
    resp = client.post("/login", data={"password": "wrong-password"},
                       follow_redirects=False)
    assert resp.status_code == 401

    # Correct password → 303 to dashboard + session cookie
    resp = client.post("/login", data={"password": "supersecret9"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "gateway_session=" in resp.headers.get("set-cookie", "")


def test_session_expiry_rejected(client, ui_env, monkeypatch):
    from ui import auth as auth_mod
    token = auth_mod.create_session_token()
    assert auth_mod.validate_session_token(token) == "admin"
    assert auth_mod.validate_session_token("tampered" + token) is None


# ---------------------------------------------------------------------------
# Dashboard & model status (test_ui_dashboard.py, test_ui_model_status.py)
# ---------------------------------------------------------------------------

def _login(client):
    client.post("/setup", data={"password": "supersecret9", "confirm": "supersecret9"})
    resp = client.post("/login", data={"password": "supersecret9"}, follow_redirects=True)
    assert resp.status_code == 200


def test_dashboard_lists_registry_models(client):
    _login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "meta/llama-3.1-8b-instruct" in resp.text
    assert "nvidia" in resp.text


def test_dashboard_shows_guidance_when_no_credentials(client, monkeypatch):
    _login(client)
    for k in ("NVIDIA_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Configure API Keys" in resp.text


def test_dashboard_omits_guidance_when_credentials_present(client, monkeypatch):
    _login(client)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_123")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Configure API Keys" not in resp.text


def test_dashboard_reflects_circuit_state_from_redis(client, ui_env, monkeypatch):
    _login(client)

    class FakeRedis:
        def __init__(self):
            self.store = {}

        def get(self, key):
            return self.store.get(key)

        def set(self, key, value, ex=None):
            self.store[key] = value
            return True

    r = FakeRedis()
    r.set("gateway:model:nvidia/meta/llama-3.1-8b-instruct:circuit", "open")
    r.set("gateway:model:nvidia/meta/llama-3.1-8b-instruct:score", "-1")
    r.set("gateway:model:nvidia/meta/llama-3.1-8b-instruct:status", "healthy")

    monkeypatch.setattr(ui_env, "get_redis", lambda: r)
    html = client.get("/").text
    assert "red" in html          # open circuit → red indicator
    assert "open" in html         # circuit state surfaced


# ---------------------------------------------------------------------------
# Stats (test_ui_stats.py)
# ---------------------------------------------------------------------------

def test_stats_shows_nonzero_requests(client):
    _login(client)
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert "10" in resp.text       # seeded request_count


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def test_logs_page_renders_metadata_only(client, ui_env):
    _login(client)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from schemas.db import RequestLog

    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as db:
        db.add(RequestLog(virtual_model="auto-free", actual_model="nvidia-auto",
                          provider="nvidia", status="success", latency_ms=123,
                          input_tokens=10, output_tokens=5))
        db.commit()
    html = client.get("/logs").text
    assert "auto-free" in html and "success" in html


# ---------------------------------------------------------------------------
# Custom providers (test_ui_custom_provider.py)
# ---------------------------------------------------------------------------

def test_add_provider_with_valid_probe(ui_env, monkeypatch):
    client = TestClient(ui_env.app)
    _login(client)

    # Mock probes: discovery returns a model, chat probe succeeds
    from wizard import provider_probe
    monkeypatch.setattr("ui.app.list_models",
                        lambda **kw: provider_probe.ProbeResult(ok=True, models=["vendor-model-a"]))
    monkeypatch.setattr("ui.app.probe_provider",
                        lambda **kw: provider_probe.ProbeResult(ok=True, status_code=200))

    resp = client.post("/providers/add", data={
        "name": "acme", "base_url": "https://api.acme.dev/v1",
        "tier": "free", "capabilities": "general",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "added+successfully" in resp.headers["location"]

    # Provider persisted in canonical config
    from schemas.config import GatewayConfig
    cfg = GatewayConfig.load_from_file(os.environ["GATEWAY_CONFIG_PATH"])
    assert [p.name for p in cfg.custom_providers] == ["acme"]
    assert cfg.custom_providers[0].models == ["vendor-model-a"]


def test_add_provider_with_failed_probe_not_added(ui_env, monkeypatch):
    client = TestClient(ui_env.app)
    _login(client)

    from wizard import provider_probe
    monkeypatch.setattr("ui.app.list_models",
                        lambda **kw: provider_probe.ProbeResult(ok=False, status_code=503,
                                                                error="probe returned HTTP 503"))
    resp = client.post("/providers/add", data={
        "name": "badco", "base_url": "https://api.badco.dev/v1", "tier": "free",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "failed" in resp.headers["location"]

    from schemas.config import GatewayConfig
    cfg = GatewayConfig.load_from_file(os.environ["GATEWAY_CONFIG_PATH"])
    assert all(p.name != "badco" for p in cfg.custom_providers)


def test_toggle_custom_provider(ui_env, monkeypatch):
    client = TestClient(ui_env.app)
    _login(client)

    from wizard import provider_probe
    monkeypatch.setattr("ui.app.list_models",
                        lambda **kw: provider_probe.ProbeResult(ok=True, models=["m1"]))
    monkeypatch.setattr("ui.app.probe_provider",
                        lambda **kw: provider_probe.ProbeResult(ok=True, status_code=200))
    client.post("/providers/add", data={
        "name": "toggleme", "base_url": "https://t.dev/v1", "tier": "free",
    })

    resp = client.post("/providers/toggleme/toggle", follow_redirects=False)
    assert resp.status_code == 303
    from schemas.config import GatewayConfig
    cfg = GatewayConfig.load_from_file(os.environ["GATEWAY_CONFIG_PATH"])
    assert cfg.custom_providers[0].enabled is False

    client.post("/providers/toggleme/toggle")
    cfg = GatewayConfig.load_from_file(os.environ["GATEWAY_CONFIG_PATH"])
    assert cfg.custom_providers[0].enabled is True


def test_custom_provider_flows_into_litellm_model_list(ui_env, monkeypatch):
    client = TestClient(ui_env.app)
    _login(client)
    from wizard import provider_probe
    monkeypatch.setattr("ui.app.list_models",
                        lambda **kw: provider_probe.ProbeResult(ok=True, models=["vm-x"]))
    monkeypatch.setattr("ui.app.probe_provider",
                        lambda **kw: provider_probe.ProbeResult(ok=True, status_code=200))
    client.post("/providers/add", data={
        "name": "flowco", "base_url": "https://f.dev/v1", "tier": "free",
    })
    from gateway.config_generator import get_models_from_registry
    from schemas.config import GatewayConfig
    cfg = GatewayConfig.load_from_file(os.environ["GATEWAY_CONFIG_PATH"])
    names = [m["model_name"] for m in get_models_from_registry(cfg)]
    assert "flowco-auto" in names


# ---------------------------------------------------------------------------
# Model toggle & probe tests (Fix 2 & Fix 3)
# ---------------------------------------------------------------------------

def test_model_toggle_disables_then_enables(client, ui_env):
    _login(client)
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from schemas.db import ModelRegistry

    engine = create_engine(os.environ["DATABASE_URL"])

    resp = client.post(
        "/models/nvidia/meta%2Fllama-3.1-8b-instruct/toggle",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session(engine) as db:
        row = db.execute(
            select(ModelRegistry).where(
                ModelRegistry.provider == "nvidia",
                ModelRegistry.model_name == "meta/llama-3.1-8b-instruct",
            )
        ).scalar_one()
        assert row.enabled is False

    client.post("/models/nvidia/meta%2Fllama-3.1-8b-instruct/toggle")
    with Session(engine) as db:
        row = db.execute(
            select(ModelRegistry).where(
                ModelRegistry.provider == "nvidia",
                ModelRegistry.model_name == "meta/llama-3.1-8b-instruct",
            )
        ).scalar_one()
        assert row.enabled is True


def test_model_toggle_unknown_returns_404(client):
    _login(client)
    resp = client.post(
        "/models/nvidia/nonexistent-model/toggle",
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_probe_endpoint_returns_202_or_303(client, ui_env):
    _login(client)
    resp = client.post("/models/probe", follow_redirects=False)
    assert resp.status_code in (202, 303)


# ---------------------------------------------------------------------------
# Builtin provider toggle & Config page tests (Fix 4 & Fix 5)
# ---------------------------------------------------------------------------

def test_builtin_provider_toggle_disables_groq(client, ui_env):
    _login(client)
    from schemas.config import GatewayConfig

    resp = client.post("/providers/groq/toggle", follow_redirects=False)
    assert resp.status_code == 303

    cfg = GatewayConfig.load_from_file(os.environ["GATEWAY_CONFIG_PATH"])
    assert cfg.providers.groq.enabled is False

    client.post("/providers/groq/toggle")
    cfg = GatewayConfig.load_from_file(os.environ["GATEWAY_CONFIG_PATH"])
    assert cfg.providers.groq.enabled is True


def test_builtin_provider_toggle_unknown_returns_404(client):
    _login(client)
    resp = client.post("/providers/nonexistent_xyz/toggle", follow_redirects=False)
    assert resp.status_code == 404


def test_config_page_shows_providers_and_credentials(client):
    _login(client)
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "Built-in providers" in resp.text
    assert "API Keys" in resp.text


def test_providers_url_serves_config(client):
    _login(client)
    resp = client.get("/providers", follow_redirects=False)
    assert resp.status_code == 200
    assert "Built-in providers" in resp.text


def test_credentials_url_serves_config(client):
    _login(client)
    resp = client.get("/credentials", follow_redirects=False)
    assert resp.status_code == 200
    assert "API Keys" in resp.text


# ---------------------------------------------------------------------------
# Logs filter tests (Fix 6)
# ---------------------------------------------------------------------------

def _seed_logs(engine, entries):
    from schemas.db import RequestLog
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        for e in entries:
            db.add(RequestLog(**e))
        db.commit()


def test_logs_filter_by_status(client, ui_env):
    _login(client)
    from sqlalchemy import create_engine
    engine = create_engine(os.environ["DATABASE_URL"])
    _seed_logs(engine, [
        {"virtual_model": "auto-free", "actual_model": "nvidia-auto",
         "provider": "nvidia", "status": "success"},
        {"virtual_model": "auto-free", "actual_model": "groq-auto-free",
         "provider": "groq", "status": "error"},
    ])
    html = client.get("/logs?status=error").text
    assert "groq-auto-free" in html
    assert "nvidia-auto" not in html


def test_logs_filter_by_provider(client, ui_env):
    _login(client)
    from sqlalchemy import create_engine
    engine = create_engine(os.environ["DATABASE_URL"])
    _seed_logs(engine, [
        {"virtual_model": "auto-free", "actual_model": "nvidia-auto",
         "provider": "nvidia", "status": "success"},
        {"virtual_model": "auto-free", "actual_model": "groq-auto-free",
         "provider": "groq", "status": "success"},
    ])
    html = client.get("/logs?provider=nvidia").text
    assert "nvidia-auto" in html
    assert "groq-auto-free" not in html


def test_logs_search_by_model_name(client, ui_env):
    _login(client)
    from sqlalchemy import create_engine
    engine = create_engine(os.environ["DATABASE_URL"])
    _seed_logs(engine, [
        {"virtual_model": "auto-free", "actual_model": "nvidia-auto",
         "provider": "nvidia", "status": "success"},
        {"virtual_model": "auto-reasoning-free", "actual_model": "cerebras-reasoning-free",
         "provider": "cerebras", "status": "success"},
    ])
    html = client.get("/logs?q=cerebras").text
    assert "cerebras-reasoning-free" in html
    assert "nvidia-auto" not in html


def test_logs_pagination_preserves_filters(client, ui_env):
    _login(client)
    from sqlalchemy import create_engine
    engine = create_engine(os.environ["DATABASE_URL"])
    _seed_logs(engine, [
        {"virtual_model": "auto-free", "actual_model": f"model-{i}",
         "provider": "nvidia", "status": "error"}
        for i in range(60)
    ])
    html = client.get("/logs?status=error&page=1").text
    assert "status=error" in html
    assert "page=2" in html

