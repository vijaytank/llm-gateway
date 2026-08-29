"""
ui/auth.py — Admin authentication for the web UI (Phase 4 deliverable 2).

- Single admin password, bcrypt-hashed, stored in Postgres `ui_settings`.
- Sessions via signed cookies (itsdangerous), 24h max expiry.
- First-run: if no password hash exists, the UI serves a set-password page.

Security notes (plan Issue 12 / Phase 5 hooks):
- The session secret comes from the environment (SESSION_SECRET); if unset a
  random one is generated per boot, which invalidates sessions on restart —
  operators should set it in .env.
- Passwords are never logged or stored in plaintext.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from schemas.db import UiSetting

PASSWORD_HASH_KEY = "admin_password_hash"
SESSION_MAX_AGE_SECONDS = 24 * 3600  # 24 hours max (plan security review)
SESSION_COOKIE_NAME = "gateway_session"
SESSION_SECRET_ENV = "SESSION_SECRET"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def get_password_hash(db: DbSession) -> str | None:
    row = db.get(UiSetting, PASSWORD_HASH_KEY)
    return row.value if row else None


def set_password(db: DbSession, password: str) -> None:
    """Set or replace the admin password (bcrypt-hashed)."""
    if len(password) < 8:
        raise ValueError("admin password must be at least 8 characters")
    row = db.get(UiSetting, PASSWORD_HASH_KEY)
    value = hash_password(password)
    if row is None:
        db.add(UiSetting(key=PASSWORD_HASH_KEY, value=value))
    else:
        row.value = value
    db.commit()


_per_boot_secret: bytes | None = None
_session_secret_warned = False


def get_session_secret() -> bytes:
    global _per_boot_secret, _session_secret_warned
    secret = __import__("os").environ.get(SESSION_SECRET_ENV, "")
    if secret:
        return secret.encode("utf-8")
    if _per_boot_secret is None:
        if not _session_secret_warned:
            import logging
            logging.getLogger(__name__).warning(
                "SESSION_SECRET not set — sessions will not survive restarts. "
                "Set SESSION_SECRET in .env for persistent sessions."
            )
            _session_secret_warned = True
        _per_boot_secret = secrets.token_bytes(32)
    return _per_boot_secret


def create_session_token(username: str = "admin") -> str:
    signer = TimestampSigner(get_session_secret())
    return signer.sign(username.encode("utf-8")).decode("utf-8")


def validate_session_token(token: str) -> str | None:
    """Return the username if the token is valid and unexpired, else None."""
    try:
        signer = TimestampSigner(get_session_secret())
        username = signer.unsign(
            token.encode("utf-8"), max_age=SESSION_MAX_AGE_SECONDS
        ).decode("utf-8")
        return username
    except (BadSignature, SignatureExpired):
        return None


def authenticate(db: DbSession, password: str) -> bool:
    stored = get_password_hash(db)
    if stored is None:
        return False
    return verify_password(password, stored)


def needs_setup(db: DbSession) -> bool:
    return get_password_hash(db) is None
