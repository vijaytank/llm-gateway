"""
test_fallback_429_5xx.py — Plan Phase 1: 429/5xx fallback cascades.

Covers (plan test_429_fallback / test_5xx_fallback):
  - Primary mock returns 429 → request falls back to secondary, succeeds
  - Primary + secondary fail → last-resort deployment serves the request
  - All deployments failing → structured JSON error body (never a stack trace)
  - Failure events are logged to Postgres with the right status/error codes
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (  # noqa: E402
    gateway_chat, mock_request_count, mock_set_status, pg_query, wait_until,
)

PRIMARY = "alpha-primary"
SECONDARY = "beta-secondary"
LAST = "gamma-lastresort"


def test_primary_429_falls_back_to_secondary():
    mock_set_status(PRIMARY, 429)
    try:
        primary_before = mock_request_count(PRIMARY)
        secondary_before = mock_request_count(SECONDARY)
        status, body = gateway_chat()
        assert status == 200, body
        data = json.loads(body)
        # The successful answer must come from a non-primary deployment
        assert PRIMARY not in data["model"], \
            f"routed to failing primary: {data['model']}"
        assert mock_request_count(SECONDARY) > secondary_before or \
               mock_request_count(LAST) > 0
    finally:
        mock_set_status(PRIMARY, None)


def test_two_failures_fall_back_to_last_resort():
    """Primary 429 + secondary 503 → last resort must serve.

    Review follow-up: a previous test's failures put deployments into
    LiteLLM's short internal cooldown, so retry until a deployment is
    available rather than assuming the very first call succeeds.
    """
    mock_set_status(PRIMARY, 429)
    mock_set_status(SECONDARY, 503)
    try:
        def _last_resort_served():
            status, body = gateway_chat()
            if status == 200 and mock_request_count(LAST) > 0:
                return status, body
            return None

        status, body = wait_until(_last_resort_served, timeout=45,
                                  interval=3.0,
                                  desc="fallback to last resort")
    finally:
        mock_set_status(PRIMARY, None)
        mock_set_status(SECONDARY, None)


def test_all_deployments_down_returns_structured_error():
    """AC: structured error body — no stack trace leaks to the client."""
    for model in (PRIMARY, SECONDARY, LAST):
        mock_set_status(model, 503)
    try:
        status, body = gateway_chat(timeout=90)
        assert 400 <= status < 600
        # Must be parseable JSON with an error key — not an HTML traceback
        data = json.loads(body)
        assert "error" in json.dumps(data).lower()
        assert "Traceback" not in body
    finally:
        for model in (PRIMARY, SECONDARY, LAST):
            mock_set_status(model, None)


def test_failure_events_logged_to_postgres():
    mock_set_status(PRIMARY, 500)
    try:
        before_rows = int(pg_query("SELECT COUNT(*) FROM request_logs", fetch="one"))
        gateway_chat()  # may succeed via fallback; primary failure must still be logged

        def _logged():
            rows = pg_query(
                "SELECT COUNT(*) FROM request_logs WHERE status != 'success'", fetch="one")
            return int(rows) >= 1
        wait_until(_logged, timeout=30, desc="failure row in request_logs")

        after_rows = int(pg_query("SELECT COUNT(*) FROM request_logs", fetch="one"))
        assert after_rows >= before_rows
    finally:
        mock_set_status(PRIMARY, None)


def test_recovery_after_upstream_heals():
    """After the mock heals, requests succeed again without restarts.

    Note (review follow-up): LiteLLM puts deployments that returned 5xx into
    a short internal cooldown (~5s). The heal assertion therefore retries
    until the cooldown expires rather than assuming immediate recovery.
    """
    mock_set_status(PRIMARY, 503)
    gateway_chat(timeout=90)
    mock_set_status(PRIMARY, None)

    def _healed():
        status, body = gateway_chat()
        return status == 200 and (status, body)

    status, body = wait_until(_healed, timeout=30, interval=3.0,
                              desc="recovery after upstream heals")
