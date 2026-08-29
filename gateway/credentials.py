"""gateway/credentials.py — DB-backed provider API key resolution (P1.2.3).

Gateway reads provider API keys from the provider_credentials table
(encrypted at rest, decrypted on read) instead of only from env vars.

Resolution order:
  1. Process cache (60s TTL) — avoids hammering Postgres on config regen.
  2. provider_credentials row (decrypted via schemas.db.decrypt_api_key).
  3. Env var fallback (FR-1.2.4) — backward-compatible with the classic
     .env workflow (NVIDIA_API_KEY, etc.); also the degraded-mode path when
     the DB is unreachable/unmigrated (P1.1).

No API keys are ever logged or cached outside this module's in-memory dict.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

_CACHE_TTL_SECONDS = 60

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _read_from_db(provider_name: str) -> Optional[str]:
    """Return the decrypted key for a provider, or None if absent/unreachable.

    Imported lazily so this module can be imported even when SQLAlchemy or the
    DB is unavailable (degraded mode, unit-test isolation).
    """
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from schemas.db import ProviderCredential, decrypt_api_key

        url = os.environ.get("GATEWAY_DB_URL") or os.environ.get("DATABASE_URL", "")
        if not url:
            return None
        engine = create_engine(url)
        with Session(engine) as session:
            row = (
                session.query(ProviderCredential)
                .filter_by(provider_name=provider_name, is_active=True)
                .first()
            )
            if row is None:
                return None
            return decrypt_api_key(row.api_key_encrypted)
    except Exception as e:  # pragma: no cover - defensive degraded-mode path
        print(f"[credentials] DB lookup failed for '{provider_name}': {e.__class__.__name__}: {e}")
        return None


def get_provider_api_key(
    provider_name: str, api_key_env: Optional[str] = None
) -> Optional[str]:
    """Resolve a provider's API key (DB first, then env var).

    `api_key_env` is the fallback env var name (e.g. "NVIDIA_API_KEY"). An
    empty-string env value is treated as unset (matches classic behavior).
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(provider_name)
        if hit and now - hit["ts"] < _CACHE_TTL_SECONDS:
            return hit["key"]

    key = _read_from_db(provider_name)
    if key is None and api_key_env:
        key = os.environ.get(api_key_env) or None

    with _cache_lock:
        _cache[provider_name] = {"key": key, "ts": now}
    return key


def invalidate_cache(provider_name: Optional[str] = None) -> None:
    """Drop the cached key for one provider, or the whole cache when None.

    Called by the UI after a credential write so the gateway picks up the
    change without waiting out the TTL (plan FR-1.2.3).
    """
    with _cache_lock:
        if provider_name:
            _cache.pop(provider_name, None)
        else:
            _cache.clear()
