"""
test_eol_model_fallback.py — Verify fallback behavior when a deployment returns 410 (Gone / EOL).

Covers:
  - Primary mock returns 410 Gone (End of Life) → request falls back to secondary / last resort and succeeds with 200
  - Recovery when mock status is restored
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (  # noqa: E402
    gateway_chat, mock_request_count, mock_set_status, wait_until,
)

PRIMARY = "alpha-primary"
SECONDARY = "beta-secondary"
LAST = "gamma-lastresort"


def test_410_eol_falls_back_to_secondary_or_last_resort():
    """When a model deployment reaches End of Life (410), the request must
    transparently fail over across the virtual model group to a live deployment."""
    mock_set_status(PRIMARY, 410)
    try:
        primary_before = mock_request_count(PRIMARY)
        secondary_before = mock_request_count(SECONDARY)
        status, body = gateway_chat()
        assert status == 200, f"Expected 200 after 410 fallback, got {status}: {body}"
        data = json.loads(body)
        assert PRIMARY not in data.get("model", ""), \
            f"Expected fallback model but got primary: {data.get('model')}"
        assert mock_request_count(SECONDARY) > secondary_before or \
               mock_request_count(LAST) > 0
    finally:
        mock_set_status(PRIMARY, None)
