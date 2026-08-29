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

import logging
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, select, desc, func
from sqlalchemy.orm import Session as DbSession, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schemas.config import GatewayConfig, CustomProviderConfig, BUILTIN_PROVIDERS
from schemas.db import (
    Base,
    ModelRegistry,
    ModelStatsHourly,
    ProviderCredential,
    RequestLog,
    UiSetting,
    decrypt_api_key,
    encrypt_api_key,
    rotate_encryption_key,
)
from ui import auth as ui_auth
from wizard.provider_probe import list_models, probe_provider

# P1.2.2: gateway credential cache is invalidated on UI write so the gateway
# picks up changes without waiting out the 60s TTL.
from gateway.credentials import invalidate_cache

app = FastAPI(title="LLM Gateway UI", docs_url=None, redoc_url=None)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database / Redis wiring (env-driven, no hardcoded URLs)
# ---------------------------------------------------------------------------

def _database_url() -> str:
    """Resolve at call time so tests/deployments can repoint DATABASE_URL.
    GATEWAY_DB_URL is preferred (DATABASE_URL in a LiteLLM process enables
    its Prisma layer, which resets the shared schema)."""
    return os.environ.get("GATEWAY_DB_URL") or os.environ.get("DATABASE_URL", "")


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


# Phase 5 security: shared login rate limiter (5 failures/min → 429).
from ui.rate_limit import LoginRateLimiter, client_ip_from_request  # noqa: E402

# Lazily initialized on first login request so Redis unavailability at startup
# does not prevent the module from importing (e.g. test collection, bare-metal starts).
_login_limiter: LoginRateLimiter | None = None


def _get_login_limiter() -> LoginRateLimiter:
    global _login_limiter
    if _login_limiter is None:
        _login_limiter = LoginRateLimiter(redis_client=get_redis())
    return _login_limiter


def _login_limiter_client_ip(request: Request) -> str:
    return client_ip_from_request(request)


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


def require_admin(request: Request):
    """Dependency that enforces admin authentication (P1.3).

    Returns 403 for API-style endpoints (vs the middleware's redirect to /login).
    Use on POST routes where a redirect is not appropriate.
    """
    token = request.cookies.get(ui_auth.SESSION_COOKIE_NAME, "")
    if not token or ui_auth.validate_session_token(token) is None:
        raise HTTPException(status_code=403, detail="Admin authentication required")


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
    # Phase 5 security: rate-limit login attempts (5 failures/min → 429).
    limiter = _get_login_limiter()
    client_ip = _login_limiter_client_ip(request)
    if not limiter.check_allowed(client_ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many login attempts. Try again in a minute."},
            status_code=429,
        )
    db = new_db_session()
    try:
        if not ui_auth.authenticate(db, password):
            # Phase 5: failed attempt feeds the sliding-window limiter.
            limiter.record_failure(client_ip)
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid password."},
                status_code=401,
            )
        limiter.reset(client_ip)
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
    db = new_db_session()
    try:
        if not ui_auth.needs_setup(db):
            return RedirectResponse(url="/login", status_code=303)
    finally:
        db.close()
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup")
def setup_submit(request: Request, password: str = Form(...), confirm: str = Form(...)):
    limiter = _get_login_limiter()
    client_ip = _login_limiter_client_ip(request)
    if not limiter.check_allowed(client_ip):
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Too many attempts. Try again in a minute."},
            status_code=429,
        )
    if password != confirm:
        limiter.record_failure(client_ip)
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Passwords do not match."}, status_code=400,
        )
    db = new_db_session()
    try:
        ui_auth.set_password(db, password)
        limiter.reset(client_ip)
    except ValueError as e:
        limiter.record_failure(client_ip)
        return templates.TemplateResponse(
            request, "setup.html", {"error": str(e)}, status_code=400,
        )
    finally:
        db.close()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession = Depends(get_db)):
    rows = []
    db_error = False
    try:
        rows = db.execute(select(ModelRegistry).order_by(ModelRegistry.provider, ModelRegistry.model_name)).scalars().all()
    except Exception:
        logger.warning("dashboard: failed to query ModelRegistry", exc_info=True)
        db_error = True

    try:
        r = get_redis()
    except Exception:
        r = None

    models = []
    for row in rows:
        entry = model_status_from_redis(r, f"{row.provider}/{row.model_name}") if r else {
            "status": "unknown", "circuit": "unknown", "score": None, "last_success": None
        }
        models.append({
            "provider": row.provider,
            "model_name": row.model_name,
            "tier": row.tier,
            "enabled": row.enabled,
            "capabilities": row.capabilities or [],
            **entry,
            "color": health_color(entry),
        })

    # Check if any credentials are configured (UX-0.3 first-visit guidance)
    has_credentials = False
    try:
        creds = db.execute(select(ProviderCredential)).scalars().all()
        if creds:
            has_credentials = True
    except Exception:
        logger.warning("dashboard: failed to query ProviderCredential", exc_info=True)

    if not has_credentials:
        for name in BUILTIN_PROVIDERS:
            env_key = f"{name.upper()}_API_KEY"
            if os.environ.get(env_key):
                has_credentials = True
                break

    return templates.TemplateResponse(request, "dashboard.html", {
        "models": models,
        "has_credentials": has_credentials,
        "db_error": db_error,
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
def providers_add(
    name: str = Form(...),
    base_url: str = Form(...),
    api_key_env: str = Form(""),
    tier: str = Form("free"),
    capabilities: str = Form("general"),
    discover_models: bool = Form(False),
):
    """Add a custom provider. The provider is probed live before saving;
    a failed probe returns an error and nothing is persisted."""
    # Probe FIRST (review F-M7): construct the CustomProviderConfig only once
    # real model names exist — placeholder models can no longer be constructed.
    discovery = list_models(base_url=base_url.strip(), auth_type="bearer",
                            api_key_env=api_key_env.strip(), timeout=12.0)
    if not discovery.ok or not discovery.models:
        return providers_add_error(
            f"Model discovery failed ({discovery.error}); add the provider "
            "with a reachable /v1/models endpoint."
        )

    try:
        cp = CustomProviderConfig(
            name=name.strip().lower(),
            base_url=base_url.strip(),
            api_key_env=api_key_env.strip(),
            tier=tier,
            capabilities=[c.strip() for c in capabilities.split(",") if c.strip()],
            models=discovery.models,
        )
    except Exception as e:
        return providers_add_error(f"Invalid provider: {e}")

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
        url=f"/providers?msg={urllib.parse.quote_plus(message)}",
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


# ---------------------------------------------------------------------------
# API Keys (P1.2.2) — manage provider credentials stored encrypted in Postgres.
# ---------------------------------------------------------------------------

# Provider display order (builtin first), matching the dashboard/provider pages.
_CRED_PROVIDERS = BUILTIN_PROVIDERS


def _mask_api_key(key: str) -> str:
    """Mask a key for display: show first 4 + last 4 chars only (FR-1.2.5).
    Never returns the full key."""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _build_provider_cred_entry(name: str, row) -> dict:
    """Build a display dict for one provider credential (masked, never full key)."""
    configured = row is not None
    masked = ""
    if configured:
        try:
            masked = _mask_api_key(decrypt_api_key(row.api_key_encrypted))
        except Exception:
            masked = "****"
    return {"name": name, "configured": configured, "masked": masked}


@app.get("/credentials", response_class=HTMLResponse)
def credentials_page(request: Request, db: DbSession = Depends(get_db)):
    """List builtin providers and their configured status (masked, never full)."""
    rows = db.execute(select(ProviderCredential)).scalars().all()
    creds = {r.provider_name: r for r in rows}
    providers = [_build_provider_cred_entry(name, creds.get(name)) for name in _CRED_PROVIDERS]
    # Custom providers (from canonical config) get the same treatment.
    config = load_gateway_config()
    for cp in config.custom_providers:
        providers.append(_build_provider_cred_entry(cp.name, creds.get(cp.name)))
    return templates.TemplateResponse(request, "credentials.html", {
        "providers": providers,
        "message": request.query_params.get("msg", ""),
    })


@app.post("/credentials/{name}")
def credentials_set(request: Request, name: str, api_key: str = Form(...), db: DbSession = Depends(get_db)):
    """Store (or update) an encrypted API key for a provider (FR-1.2.1)."""
    require_admin(request)
    if not name.strip() or not api_key.strip():
        raise HTTPException(status_code=400, detail="provider name and api_key are required")
    row = db.execute(select(ProviderCredential).where(ProviderCredential.provider_name == name)).scalar_one_or_none()
    encrypted = encrypt_api_key(api_key)
    if row is None:
        row = ProviderCredential(provider_name=name, api_key_encrypted=encrypted)
        db.add(row)
    else:
        row.api_key_encrypted = encrypted
        row.is_active = True
    db.commit()
    invalidate_cache(name)
    return RedirectResponse(url=f"/credentials?msg=API+key+saved+for+{name}.", status_code=303)


@app.post("/credentials/{name}/delete")
def credentials_delete(request: Request, name: str, db: DbSession = Depends(get_db)):
    """Remove a stored credential for a provider (FR-1.2.6 / AC-SEC-06)."""
    require_admin(request)
    row = db.execute(select(ProviderCredential).where(ProviderCredential.provider_name == name)).scalar_one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()
    invalidate_cache(name)
    return RedirectResponse(url=f"/credentials?msg=API+key+removed+for+{name}.", status_code=303)


# ---------------------------------------------------------------------------
# Security Settings (P1.3) — credential rotation.
# ---------------------------------------------------------------------------

def _update_env_file(key: str, value: str) -> bool:
    """Update a key=value pair in the project-root .env file (and docker/.env if present).

    Returns True if the file was modified, False if the key was not found
    (in which case it is appended). Creates the file if it does not exist.
    Uses atomic write-to-temp-then-replace.
    """
    targets = [ROOT / ".env"]
    docker_env = ROOT / "docker" / ".env"
    if docker_env.parent.exists() and docker_env != targets[0]:
        targets.append(docker_env)

    modified = False
    for env_path in targets:
        try:
            if env_path.exists():
                lines = env_path.read_text().splitlines()
                found = False
                new_lines = []
                for line in lines:
                    if line.startswith(f"{key}="):
                        new_lines.append(f"{key}={value}")
                        found = True
                    else:
                        new_lines.append(line)
                if not found:
                    new_lines.append(f"{key}={value}")
                content = "\n".join(new_lines) + "\n"
            elif env_path.parent.exists():
                content = f"{key}={value}\n"
            else:
                continue

            tmp_path = env_path.with_name(f"{env_path.name}.tmp.{os.getpid()}")
            tmp_path.write_text(content)
            tmp_path.replace(env_path)
            modified = True
        except Exception:
            pass
    return modified


@app.get("/security", response_class=HTMLResponse)
def security_page(request: Request, db: DbSession = Depends(get_db)):
    """Security Settings page — shows current status of rotatable credentials."""
    # Check which credentials are configured
    admin_hash = db.get(UiSetting, ui_auth.PASSWORD_HASH_KEY)
    env_path = ROOT / ".env"
    env_configured = {}
    if env_path.exists():
        env_content = env_path.read_text()
        for key in ("LITELLM_MASTER_KEY", "SESSION_SECRET", "SECRET_ENCRYPTION_KEY"):
            for line in env_content.splitlines():
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip()
                    env_configured[key] = bool(val)
                    break
            else:
                env_configured[key] = False
    return templates.TemplateResponse(request, "security.html", {
        "admin_password_set": admin_hash is not None,
        "litellm_master_key_set": env_configured.get("LITELLM_MASTER_KEY", False),
        "session_secret_set": env_configured.get("SESSION_SECRET", False),
        "encryption_key_set": env_configured.get("SECRET_ENCRYPTION_KEY", False),
        "message": request.query_params.get("msg", ""),
        "error": request.query_params.get("err", ""),
    })


@app.post("/security/rotate-admin-password")
def security_rotate_admin_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: DbSession = Depends(get_db),
):
    """Rotate the admin password (FR-1.3.1, AC-P1.3.2)."""
    require_admin(request)
    # Verify current password first
    if not ui_auth.authenticate(db, current_password):
        return RedirectResponse(
            url="/security?err=Current+password+is+incorrect", status_code=303
        )
    if new_password != confirm_password:
        return RedirectResponse(
            url="/security?err=New+passwords+do+not+match", status_code=303
        )
    try:
        ui_auth.set_password(db, new_password)
    except ValueError as e:
        return RedirectResponse(url=f"/security?err={str(e).replace(' ', '+')}", status_code=303)
    return RedirectResponse(url="/security?msg=Admin+password+updated+successfully", status_code=303)


@app.post("/security/rotate-master-key")
def security_rotate_master_key(
    request: Request,
    new_master_key: str = Form(...),
    confirm: str = Form(...),
):
    """Rotate the LiteLLM master key (FR-1.3.1, AC-P1.3.3).

    Writes the new key to .env. Gateway restart required to apply.
    """
    require_admin(request)
    if new_master_key != confirm:
        return RedirectResponse(url="/security?err=Keys+do+not+match", status_code=303)
    if len(new_master_key) < 8:
        return RedirectResponse(
            url="/security?err=Master+key+must+be+at+least+8+characters", status_code=303
        )
    if not _update_env_file("LITELLM_MASTER_KEY", new_master_key):
        return RedirectResponse(
            url="/security?err=Master+key+rotation+failed:+could+not+write+.env",
            status_code=303,
        )
    return RedirectResponse(
        url="/security?msg=Master+key+updated.+Restart+gateway+to+apply.",
        status_code=303,
    )


@app.post("/security/rotate-session-secret")
def security_rotate_session_secret(request: Request):
    """Generate a new session secret (FR-1.3.1, AC-P1.3.4).

    Writes the new secret to .env. All existing sessions are invalidated
    (they were signed with the old secret). Gateway restart required.
    """
    require_admin(request)
    import secrets as _secrets
    new_secret = _secrets.token_hex(32)
    if not _update_env_file("SESSION_SECRET", new_secret):
        return RedirectResponse(
            url="/security?err=Session+secret+rotation+failed:+could+not+write+.env",
            status_code=303,
        )
    return RedirectResponse(
        url="/security?msg=Session+secret+updated.+All+sessions+invalidated.+Restart+gateway+to+apply.",
        status_code=303,
    )


@app.post("/security/rotate-encryption-key")
def security_rotate_encryption_key(request: Request, db: DbSession = Depends(get_db)):
    """Rotate the encryption key (FR-1.3.1, AC-P1.3.5).

    Re-encrypts all stored API keys with the new key. The new key is written
    to .env. Uses the rotate_encryption_key() function from schemas.db.
    """
    require_admin(request)
    from cryptography.fernet import Fernet as _Fernet
    new_key = _Fernet.generate_key().decode("utf-8")
    try:
        rotate_encryption_key(db, new_key)
    except Exception as e:
        return RedirectResponse(
            url=f"/security?err=Key+rotation+failed:+{str(e).replace(' ', '+')}",
            status_code=303,
        )
    if not _update_env_file("SECRET_ENCRYPTION_KEY", new_key):
        return RedirectResponse(
            url="/security?err=Encryption+key+written+to+DB+but+.env+update+failed:+update+SECRET_ENCRYPTION_KEY+manually",
            status_code=303,
        )
    return RedirectResponse(
        url="/security?msg=Encryption+key+rotated.+All+stored+API+keys+re-encrypted.",
        status_code=303,
    )


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
