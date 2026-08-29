"""
test_security_rotation.py — Plan Phase 1.3: End-to-end security credential rotation against the live stack.

Covers:
  - Security settings page rendering (/security)
  - Admin password rotation (/security/rotate-admin-password) with login validation and teardown restore
  - LiteLLM master key rotation (/security/rotate-master-key)
  - Session secret rotation (/security/rotate-session-secret)
  - Fernet encryption key rotation (/security/rotate-encryption-key) with credential re-encryption
"""

import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import UI_URL, pg_query  # noqa: E402
from test_ui_integration import ADMIN_PASSWORD, UiClient, _ensure_logged_in  # noqa: E402


def test_security_page_renders_with_status(docker_stack):
    ui = UiClient()
    _ensure_logged_in(ui)
    status, _, body = ui.request("GET", "/security")
    assert status == 200
    assert "Security Settings" in body
    assert "Admin Password" in body
    assert "LiteLLM Master Key" in body
    assert "Session Secret" in body
    assert "Encryption Key" in body


def test_rotate_admin_password_flow(docker_stack):
    ui = UiClient()
    _ensure_logged_in(ui)

    temp_password = ADMIN_PASSWORD + "-rotated1"

    # 1. Wrong current password fails
    status, headers, _ = ui.request(
        "POST",
        "/security/rotate-admin-password",
        data={
            "current_password": "wrong-current-password",
            "new_password": temp_password,
            "confirm_password": temp_password,
        },
    )
    assert status in (200, 303)
    loc = urllib.parse.unquote_plus(headers.get("Location", headers.get("location", "")))
    assert "incorrect" in loc.lower() or "err=" in loc.lower()

    # 2. Correct rotation
    status, headers, _ = ui.request(
        "POST",
        "/security/rotate-admin-password",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_password": temp_password,
            "confirm_password": temp_password,
        },
    )
    assert status in (200, 303)

    # 3. Verify old password rejected
    fresh_ui = UiClient()
    status, _, _ = fresh_ui.request("POST", "/login", data={"password": ADMIN_PASSWORD})
    assert status == 401

    # 4. Verify new password accepted
    status, _, _ = fresh_ui.request("POST", "/login", data={"password": temp_password})
    assert status in (200, 303)

    # 5. Restore original password for other integration tests
    status, _, _ = fresh_ui.request(
        "POST",
        "/security/rotate-admin-password",
        data={
            "current_password": temp_password,
            "new_password": ADMIN_PASSWORD,
            "confirm_password": ADMIN_PASSWORD,
        },
    )
    assert status in (200, 303)


def test_rotate_master_key_and_session_secret(docker_stack):
    ui = UiClient()
    _ensure_logged_in(ui)

    # Rotate master key
    new_master = "sk-litellm-integration-test-new-key-123456"
    status, headers, _ = ui.request(
        "POST",
        "/security/rotate-master-key",
        data={"new_master_key": new_master, "confirm": new_master},
    )
    assert status in (200, 303)

    # Rotate session secret
    status, headers, _ = ui.request("POST", "/security/rotate-session-secret")
    assert status in (200, 303)


def test_rotate_encryption_key_flow(docker_stack):
    ui = UiClient()
    _ensure_logged_in(ui)

    # 1. Store a test credential
    ui.request("POST", "/credentials/cerebras", data={"api_key": "csk-rot-test-key-112233"})

    # 2. Trigger encryption key rotation
    status, headers, _ = ui.request("POST", "/security/rotate-encryption-key")
    assert status in (200, 303)
    loc = urllib.parse.unquote_plus(headers.get("Location", headers.get("location", "")))
    assert "err=" not in loc, f"rotation error: {loc}"
    assert "Encryption key rotated" in loc

    # 3. Verify credential is still accessible and masked properly
    status, _, body = ui.request("GET", "/credentials")
    assert status == 200
    assert "cerebras" in body
    assert "csk-rot-test-key-112233" not in body
    assert "csk-" in body

    # 4. Cleanup
    ui.request("POST", "/credentials/cerebras/delete")
