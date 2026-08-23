"""
wizard/provider_probe.py — Validate custom providers before saving them.

Used by both the CLI wizard and the web UI (Phase 4 deliverable 1/2):
a custom provider is only persisted after a live probe succeeds.

Two probes:
  1. list_models() — GET {base_url}/v1/models (OpenAI-compatible discovery)
  2. probe_provider() — POST {base_url}/v1/chat/completions with the
     structured payload from Issue 4 (never the bare "ping" prompt).

No hardcoded endpoints: everything comes from arguments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

# Structured health-probe payload per plan Issue 4 fix.
PROBE_MESSAGES = [{"role": "user", "content": "Reply with the single word OK."}]
PROBE_MAX_TOKENS = 3


@dataclass
class ProbeResult:
    ok: bool
    status_code: int | None = None
    error: str = ""
    models: list[str] = field(default_factory=list)


def _auth_headers(auth_type: str, api_key_env: str) -> dict[str, str]:
    """Build auth headers from an env var reference — keys never live in config."""
    if not api_key_env or auth_type == "none":
        return {}
    key = os.environ.get(api_key_env, "")
    if not key:
        return {}
    if auth_type == "header":
        # api_key_env may name several vars as "ENV_NAME=Header-Name"
        if "=" in api_key_env:
            env_name, header_name = api_key_env.split("=", 1)
            return {header_name: os.environ.get(env_name, "")}
        return {"X-API-Key": key}
    return {"Authorization": f"Bearer {key}"}


def list_models(base_url: str, auth_type: str = "bearer", api_key_env: str = "",
                timeout: float = 12.0) -> ProbeResult:
    """Discover models via GET {base_url}/models (OpenAI-compatible)."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = httpx.get(
            url,
            headers=_auth_headers(auth_type, api_key_env),
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        return ProbeResult(ok=False, error=f"connection failed: {e.__class__.__name__}: {e}")
    except ValueError as e:
        return ProbeResult(ok=False, error=str(e))

    if resp.status_code != 200:
        return ProbeResult(ok=False, status_code=resp.status_code,
                           error=f"HTTP {resp.status_code} from {url}")
    try:
        body = resp.json()
    except ValueError:
        return ProbeResult(ok=False, status_code=200, error="non-JSON response")
    models = [m.get("id", "") for m in body.get("data", []) if isinstance(m, dict) and m.get("id")]
    return ProbeResult(ok=True, status_code=200, models=models)


def probe_provider(base_url: str, model: str, auth_type: str = "bearer",
                   api_key_env: str = "", timeout: float = 12.0) -> ProbeResult:
    """Chat-completion probe against a specific model (Issue 4 payload)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {"model": model, "messages": PROBE_MESSAGES, "max_tokens": PROBE_MAX_TOKENS}
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json",
                     **_auth_headers(auth_type, api_key_env)},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        return ProbeResult(ok=False, error=f"connection failed: {e.__class__.__name__}: {e}")

    # Content-filter refusals mean the endpoint is up (Issue 4 classification)
    body_text = ""
    try:
        body_text = resp.text[:500]
    except Exception:
        pass
    if resp.status_code == 400 and ("content_filter" in body_text or "moderation" in body_text):
        return ProbeResult(ok=True, status_code=400)

    if resp.status_code == 429:
        # Rate-limited at first request: endpoint reachable (Issue 4)
        return ProbeResult(ok=True, status_code=429)
    if resp.status_code in (401, 403):
        return ProbeResult(ok=False, status_code=resp.status_code, error="authentication failed — check API key")
    if resp.status_code != 200:
        return ProbeResult(ok=False, status_code=resp.status_code,
                           error=f"probe returned HTTP {resp.status_code}: {body_text[:200]}")
    return ProbeResult(ok=True, status_code=200)
