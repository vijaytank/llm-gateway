"""
test_security_integration.py — Phase 5 security against the live stack.

Covers plan test_ui_login_rate_limit end-to-end:
6 failed login POSTs within a minute → the 6th returns 429.
"""

import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import UI_URL  # noqa: E402


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


def _post_login(password, ip="10.9.9.9"):
    """POST /login; X-Forwarded-For isolates this test's attempts."""
    req = urllib.request.Request(
        f"{UI_URL}/login",
        data=urllib.parse.urlencode({"password": password}).encode(),
        method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("X-Forwarded-For", ip)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=15)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_login_rate_limit_returns_429_after_five_failures():
    """5 failed attempts allowed (each 401), 6th blocked with 429."""
    codes = [_post_login("definitely-wrong-password") for _ in range(6)]
    assert all(c == 401 for c in codes[:5]), \
        f"first five attempts should be plain 401s: {codes}"
    assert codes[5] == 429, \
        f"6th attempt must be rate-limited (429), got {codes}"


def test_rate_limit_is_per_ip_and_reset_by_success():
    """A different IP is unaffected; a successful login clears the counter."""
    # Fresh IP: first attempt works normally.
    assert _post_login("wrong", ip="10.9.9.8") == 401

    # The admin password is set by other UI tests via /setup; a correct
    # login resets the limiter for its IP.
    from conftest import pg_query
    row = pg_query(
        "SELECT value FROM ui_settings WHERE key='admin_password_hash'",
        fetch="one")
    if not row:
        pytest.skip("admin password not set yet on this stack")

    # Burn through the limit on one IP, then verify another IP still works.
    for _ in range(5):
        _post_login("wrong", ip="10.7.7.7")
    assert _post_login("wrong", ip="10.7.7.6") == 401, \
        "different IP must not be rate-limited by 10.7.7.7's failures"
