"""
ui/app.py — Minimal web UI for the LLM Gateway (Phase 4 deliverable 2).

FastAPI + Jinja2 server-rendered pages on port 4002. No React, no npm build
step, no JS bundler — plain HTML templates with minimal inline JS-free forms.

Routes (per plan):
  GET  /                        Dashboard: model status, score, circuit state
  GET  /providers               Provider list with enable/disable toggles
  GET  /stats                   24h requests/errors/latency chart (model_stats_hourly)
  GET  /logs                    Last 50 request logs, paginated (metadata only)
  POST /providers/add           Add custom provider (probed live before save)
  POST /providers/{name}/toggle Enable/disable a provider
  GET  /health                  Health check endpoint
  GET/POST /login, /logout      Admin auth (bcrypt hash in ui_settings)
  GET/POST /setup               First-run admin password setup

All state is read from the real sources: Redis for scores/circuit/status,
Postgres for registry/stats/logs, gateway_config.yaml for provider config.
No mock data anywhere.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, select, desc, func
from sqlalchemy.orm import Session as DbSession, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schemas.config import GatewayConfig, CustomProviderConfig
from schemas.db import (
    Base,
    ModelRegistry,
    ModelStatsHourly,
    RequestLog,
    UiSetting,
)
from ui import auth as ui_auth
from wizard.provider_probe import list_models, probe_provider

app = FastAPI(title="LLM Gateway UI", docs_url=None, redoc_url=None)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Database / Redis wiring (env-driven, no hardcoded URLs)
# ---------------------------------------------------------------------------

def _database_url() -> str:
    """Resolve at call time so tests/deployments can repoint DATABASE_URL."""
    return os.environ.get("DATABASE_URL", os.environ.get("GATEWAY_DB_URL", ""))


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = _database_url()
        if not url:
            raise RuntimeError("DATABASE_URL not set; UI cannot reach Postgres")
        _engine = create_engine(url, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine)
    return _engine


def get_db():
    get_engine()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_redis():
    import redis as redis_lib
    redis_url = os.environ.get("REDIS_URL", os.environ.get("GATEWAY_REDIS_URL", "redis://localhost:6379/0"))
    return redis_lib.from_url(redis_url, decode_responses=True)


def new_db_session():
    """Initialize the engine if needed and return a fresh DB session."""
    get_engine()
    return _SessionLocal()


def _config_path() -> str:
    """Resolve at call time so tests/deployments can repoint GATEWAY_CONFIG_PATH."""
    return os.environ.get("GATEWAY_CONFIG_PATH", "gateway_config.yaml")


def load_gateway_config() -> GatewayConfig:
    return GatewayConfig.load_from_file(_config_path())


def save_gateway_config(config: GatewayConfig) -> None:
    config.save_to_file(_config_path())


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

PUBLIC_PATHS = {"/login", "/setup", "/health"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    # First-run: no password set yet → force setup page
    try:
        db = new_db_session()
        if db is not None and ui_auth.needs_setup(db) and path != "/setup":
            db.close()
            return RedirectResponse(url="/setup", status_code=303)
        if db is not None:
            db.close()
    except Exception:
        pass  # DB unreachable/unmigrated: fall through to per-route error handling

    token = request.cookies.get(ui_auth.SESSION_COOKIE_NAME, "")
    if not token or ui_auth.validate_session_token(token) is None:
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers: read live routing state from Redis
# ---------------------------------------------------------------------------

def model_status_from_redis(r, model_name: str) -> dict:
    """Read score/circuit/status/last-success from Redis. Absent keys → defaults."""
    status = r.get(f"gateway:model:{model_name}:status") or "unknown"
    circuit = r.get(f"gateway:model:{model_name}:circuit") or "closed"
    score_raw = r.get(f"gateway:model:{model_name}:score")
    last_success = r.get(f"gateway:model:{model_name}:last_success")
    return {
        "status": status,
        "circuit": circuit,
        "score": float(score_raw) if score_raw is not None else None,
        "last_success": last_success,
    }


def health_color(entry: dict) -> str:
    """green/yellow/red indicator per plan dashboard spec."""
    if entry["circuit"] == "open":
        return "red"
    status = entry["status"]
    if status == "healthy":
        return "green"
    if status in ("rate_limited", "half_open", "degraded"):
        return "yellow"
    return "red"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "ui"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    db = new_db_session()
    try:
        if not ui_auth.authenticate(db, password):
            # Phase 5 will add rate limiting here; wrong password → 401 shape
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid password."},
                status_code=401,
            )
        token = ui_auth.create_session_token()
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            ui_auth.SESSION_COOKIE_NAME, token,
            max_age=ui_auth.SESSION_MAX_AGE_SECONDS, httponly=True, samesite="lax",
        )
        return resp
    finally:
        db.close()


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(ui_auth.SESSION_COOKIE_NAME)
    return resp


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    db = _SessionLocal()
    try:
        if not ui_auth.needs_setup(db):
            return RedirectResponse(url="/login", status_code=303)
    finally:
        db.close()
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup")
def setup_submit(password: str = Form(...), confirm: str = Form(...)):
    if password != confirm:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Passwords do not match."}, status_code=400,
        )
    db = new_db_session()
    try:
        ui_auth.set_password(db, password)
    except ValueError as e:
        return templates.TemplateResponse(
            request, "setup.html", {"error": str(e)}, status_code=400,
        )
    finally:
        db.close()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession = Depends(get_db)):
    rows = db.execute(select(ModelRegistry).order_by(ModelRegistry.provider, ModelRegistry.model_name)).scalars().all()
    r = get_redis()
    models = []
    for row in rows:
        entry = model_status_from_redis(r, f"{row.provider}/{row.model_name}")
        models.append({
            "provider": row.provider,
            "model_name": row.model_name,
            "tier": row.tier,
            "enabled": row.enabled,
            "capabilities": row.capabilities or [],
            **entry,
            "color": health_color(entry),
        })
    return templates.TemplateResponse(request, "dashboard.html", {
        "models": models,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    })


@app.get("/providers", response_class=HTMLResponse)
def providers_page(request: Request):
    config = load_gateway_config()
    custom = [
        {
            "name": cp.name,
            "base_url": cp.base_url,
            "tier": cp.tier,
            "enabled": cp.enabled,
            "models": cp.models,
            "source": "custom",
        }
        for cp in config.custom_providers
    ]
    builtin = [
        {"name": name, "config": pc}
        for name, pc in config.providers.model_dump().items()
    ]
    return templates.TemplateResponse(request, "providers.html", {
        "builtin": builtin,
        "custom": custom,
        "message": request.query_params.get("msg", ""),
    })


@app.post("/providers/add")
async def providers_add(
    name: str = Form(...),
    base_url: str = Form(...),
    api_key_env: str = Form(""),
    tier: str = Form("free"),
    capabilities: str = Form("general"),
    discover_models: bool = Form(False),
):
    """Add a custom provider. The provider is probed live before saving;
    a failed probe returns an error and nothing is persisted."""
    try:
        cp = CustomProviderConfig(
            name=name.strip().lower(),
            base_url=base_url.strip(),
            api_key_env=api_key_env.strip(),
            tier=tier,
            capabilities=[c.strip() for c in capabilities.split(",") if c.strip()],
            models=["__probe__"],  # placeholder to pass validation pre-discovery
        )
    except Exception as e:
        return providers_add_error(f"Invalid provider: {e}")

    # Probe: discover via /v1/models, then verify chat completion works
    discovery = list_models(base_url=cp.base_url, auth_type=cp.auth_type,
                            api_key_env=cp.api_key_env,
                            timeout=float(cp.probe_timeout_seconds))
    if discovery.ok and discovery.models:
        cp.models = discovery.models
    elif discovery.ok and discover_models:
        return providers_add_error("Model discovery returned an empty list; add models manually.")
    elif discovery.ok:
        cp.models = ["__unverified__"]
    else:
        # /models endpoint missing — try a chat probe against any user-specified model
        return providers_add_error(f"Provider probe failed: {discovery.error}")

    chat_probe = probe_provider(
        base_url=cp.base_url, model=cp.models[0], auth_type=cp.auth_type,
        api_key_env=cp.api_key_env, timeout=float(cp.probe_timeout_seconds),
    )
    if not chat_probe.ok and chat_probe.status_code != 429:
        return providers_add_error(f"Chat probe failed: {chat_probe.error}")

    config = load_gateway_config()
    if any(p.name == cp.name for p in config.custom_providers):
        return providers_add_error(f"Provider '{cp.name}' already exists.")
    config.custom_providers.append(cp)
    save_gateway_config(config)
    return RedirectResponse(url="/providers?msg=Provider+added+successfully", status_code=303)


def providers_add_error(message: str):
    return RedirectResponse(
        url=f"/providers?msg={message.replace(' ', '+').replace(':', '')}",
        status_code=303,
    )


@app.post("/providers/{name}/toggle")
def provider_toggle(name: str):
    """Toggle a custom provider's enabled flag in the canonical config."""
    config = load_gateway_config()
    for cp in config.custom_providers:
        if cp.name == name:
            cp.enabled = not cp.enabled
            save_gateway_config(config)
            return RedirectResponse(url="/providers?msg=Provider+updated", status_code=303)
    raise HTTPException(status_code=404, detail=f"provider '{name}' not found")


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, db: DbSession = Depends(get_db)):
    since = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=24)
    rows = db.execute(
        select(ModelStatsHourly)
        .where(ModelStatsHourly.hour_bucket >= since)
        .order_by(ModelStatsHourly.hour_bucket)
    ).scalars().all()

    buckets = sorted({r.hour_bucket.strftime("%H:%M") for r in rows})
    series: dict[str, list] = {}
    for key, agg in (("requests", lambda r: r.request_count),
                     ("errors", lambda r: r.error_count),
                     ("avg_latency_ms", lambda r: round(r.avg_latency_ms or 0, 1))):
        by_bucket = {}
        for r in rows:
            label = r.hour_bucket.strftime("%H:%M")
            by_bucket[label] = by_bucket.get(label, 0) + float(agg(r))
        series[key] = [by_bucket.get(b, 0) for b in buckets]

    total_requests = sum(int(r.request_count) for r in rows)
    total_errors = sum(int(r.error_count) for r in rows)
    return templates.TemplateResponse(request, "stats.html", {
        "buckets": buckets,
        "series": series,
        "total_requests": total_requests,
        "total_errors": total_errors,
        "has_data": total_requests > 0,
    })


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: DbSession = Depends(get_db), page: int = 1):
    page_size = 50
    page = max(1, page)
    total = db.execute(select(func.count(RequestLog.id))).scalar() or 0
    rows = db.execute(
        select(RequestLog)
        .order_by(desc(RequestLog.timestamp))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    entries = [{
        "timestamp": r.timestamp.strftime("%Y-%m-%d %H:%M:%S") if r.timestamp else "",
        "virtual_model": r.virtual_model,
        "actual_model": r.actual_model or "-",
        "provider": r.provider or "-",
        "status": r.status,
        "error_code": r.error_code or "",
        "latency_ms": r.latency_ms,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
    } for r in rows]
    return templates.TemplateResponse(request, "logs.html", {
        "entries": entries,
        "page": page,
        "has_prev": page > 1,
        "has_next": page * page_size < total,
        "total": total,
    })


def main() -> int:
    import uvicorn
    host = os.environ.get("UI_HOST", "0.0.0.0")
    port = int(os.environ.get("UI_PORT", "4002"))
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
