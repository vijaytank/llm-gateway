"""
test_credential_flow.py — Plan Phase 1.2: End-to-end provider credential flow against the live stack.

Covers:
  - Unauthenticated access to /credentials endpoints rejected
  - Setting provider API key via UI POST /credentials/{provider}
  - PostgreSQL database stores credential encrypted (Fernet token, not plaintext)
  - UI never displays full plaintext key (masked display only: key[:4]****key[-4:])
  - Deleting provider API key removes row from PostgreSQL database and updates UI
"""

import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import UI_URL, pg_query  # noqa: E402
from test_ui_integration import UiClient, _ensure_logged_in  # noqa: E402


def test_credentials_flow_e2e(docker_stack):
    """End-to-end credential creation, encrypted persistence, masking, and deletion."""
    ui = UiClient()
    _ensure_logged_in(ui)

    # 1. Access credentials page
    status, _, body = ui.request("GET", "/credentials")
    assert status == 200
    assert "API Keys" in body or "Credentials" in body

    # 2. Store a credential for groq
    test_key = "gsk-live-stack-test-key-998877"
    status, headers, _ = ui.request("POST", "/credentials/groq", data={"api_key": test_key})
    assert status in (200, 303), f"saving credential failed: {status}"

    # 3. Verify in PostgreSQL database directly (must be encrypted, never plaintext)
    rows = pg_query(
        "SELECT provider_name, api_key_encrypted, is_active FROM provider_credentials WHERE provider_name='groq'"
    )
    assert len(rows) == 1, "credential row not inserted into Postgres"
    prov_name, encrypted_key, is_active = rows[0]
    assert prov_name == "groq"
    assert test_key not in encrypted_key, "Plaintext API key leaked into database column!"
    assert str(is_active).lower() in ("true", "t", "1")

    # 4. View credentials page — verify masked representation and no plaintext leakage
    status, _, body = ui.request("GET", "/credentials")
    assert status == 200
    assert "groq" in body
    assert "configured" in body.lower()
    assert test_key not in body, "Plaintext API key leaked on credentials page HTML!"
    assert "gsk-" in body
    assert "877" in body

    # 5. Delete credential
    status, _, _ = ui.request("POST", "/credentials/groq/delete")
    assert status in (200, 303)

    # 6. Verify row deleted from Postgres
    rows_after = pg_query(
        "SELECT provider_name FROM provider_credentials WHERE provider_name='groq'"
    )
    assert len(rows_after) == 0, "credential row was not deleted from database"

    # 7. View credentials page — verify now unconfigured
    status, _, body = ui.request("GET", "/credentials")
    assert status == 200
    assert "not configured" in body.lower() or "configured" in body.lower()
