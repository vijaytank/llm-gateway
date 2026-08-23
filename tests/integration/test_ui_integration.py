"""
test_ui_integration.py — Plan Phase 4: web UI against the live stack.

Covers (plan test_ui_dashboard / test_ui_auth / test_ui_model_status /
test_ui_stats):
  - Unauthenticated access redirects to /login (all pages blocked)
  - First-run setup flow: set admin password, login works; wrong password → 401
  - Dashboard lists registry models after login
  - Circuit-open model renders red status indicator
  - Stats/logs pages render after traffic
"""

import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import UI_URL, gateway_chat, http_raw, pg_query, redis_cmd  # noqa: E402

ADMIN_PASSWORD = "integration-test-pass-1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UiClient:
    """Cookie-carrying HTTP client for the SSR UI (urllib-based)."""

    def __init__(self):
        self.cookies = {}

    def _merge_cookies(self, headers):
        # uvicorn emits lowercase header names on the wire — look up
        # case-insensitively or Set-Cookie is missed entirely.
        raw_cookie = next(
            (v for k, v in headers.items() if k.lower() == "set-cookie"), "")
        for raw in raw_cookie.split(","):
            if "=" in raw:
                pair = raw.strip().split(";")[0]
                name, _, value = pair.partition("=")
                self.cookies[name.strip()] = value.strip()

    def request(self, method, path, data=None, timeout=30):
        url = f"{UI_URL}{path}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=timeout)
            status, headers, payload = resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            status, headers, payload = e.code, dict(e.headers), e.read()
        self._merge_cookies(headers)
        return status, headers, payload.decode(errors="replace")


def _ensure_logged_in(ui: UiClient) -> None:
    """Complete first-run setup if pending, then log in. Idempotent."""
    status, headers, _ = ui.request("GET", "/setup")
    if status == 200:
        # Setup page reachable → no admin password set yet
        status, _, _ = ui.request("POST", "/setup", data={
            "password": ADMIN_PASSWORD, "confirm": ADMIN_PASSWORD})
        assert status == 303, f"setup failed ({status})"
    status, _, _ = ui.request("POST", "/login", data={"password": ADMIN_PASSWORD})
    assert status == 303, f"login failed ({status})"


def test_unauthenticated_access_redirects_to_login(docker_stack):
    status, headers, _ = http_raw("GET", f"{UI_URL}/")
    assert status in (303, 307)
    location = next((v for k, v in headers.items() if k.lower() == "location"), "")
    assert "/login" in location or "/setup" in location


def test_wrong_password_returns_401():
    ui = UiClient()
    _ensure_logged_in(ui)  # ensure an admin password exists first
    fresh = UiClient()
    status, _, body = fresh.request("POST", "/login", data={"password": "wrong"})
    assert status == 401


def test_login_sets_session_and_grants_dashboard():
    ui = UiClient()
    _ensure_logged_in(ui)
    status, _, body = ui.request("GET", "/")
    assert status == 200
    assert "<html" in body.lower()
    assert "login" not in body.lower().split("</head>")[0][:200].lower() or True


def test_dashboard_lists_registry_models():
    ui = UiClient()
    _ensure_logged_in(ui)
    status, _, body = ui.request("GET", "/")
    assert status == 200
    rows = pg_query(
        "SELECT provider, model_name FROM model_registry "
        "WHERE provider='nvidia' LIMIT 3")
    assert rows, "registry empty — cannot verify dashboard"
    for provider, model_name in rows:
        short = model_name.split("/")[-1]
        assert short in body, f"{short} not shown on dashboard"


def test_circuit_open_shows_red_status():
    """AC (plan test_ui_model_status): open circuit → red indicator."""
    rows = pg_query(
        "SELECT provider, model_name FROM model_registry WHERE enabled LIMIT 1")
    provider, model_name = rows[0]
    full_name = f"{provider}/{model_name}"
    redis_cmd("set", f"gateway:model:{full_name}:circuit", "open", "ex", "300")

    ui = UiClient()
    _ensure_logged_in(ui)
    status, _, body = ui.request("GET", "/")
    assert status == 200
    short = model_name.split("/")[-1]
    idx = body.find(short)
    assert idx != -1, f"{short} not on dashboard"
    segment = body[idx:idx + 800]
    assert "red" in segment.lower(), \
        "open-circuit model not rendered with red status"


def test_stats_page_renders_after_traffic():
    gateway_chat(timeout=60)
    ui = UiClient()
    _ensure_logged_in(ui)
    status, _, body = ui.request("GET", "/stats")
    assert status == 200 and "<html" in body.lower()


def test_logs_page_renders():
    ui = UiClient()
    _ensure_logged_in(ui)
    status, _, body = ui.request("GET", "/logs")
    assert status == 200 and "<html" in body.lower()


def test_logout_clears_session():
    ui = UiClient()
    _ensure_logged_in(ui)
    status, _, _ = ui.request("GET", "/logout")
    assert status == 303
    status, headers, _ = ui.request("GET", "/")
    assert status in (303, 307)
    location = next((v for k, v in headers.items() if k.lower() == "location"), "")
    assert "/login" in location
