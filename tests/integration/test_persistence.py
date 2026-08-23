"""
test_persistence.py — Plan Phase 4: data survives restarts (named volumes).

Covers (plan test_docker_data_persistence, scaled to integration runtime):
  - Requests logged → gateway+ui restarted → request_logs rows still present
  - Registry data intact after restart
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (  # noqa: E402
    compose, gateway_chat, pg_query, service_health, wait_service_healthy,
    wait_until,
)


def _count_rows():
    return int(pg_query("SELECT COUNT(*) FROM request_logs", fetch="one"))


def test_request_logs_survive_gateway_restart(docker_stack):
    # Generate traffic so there is definitely data
    for _ in range(2):
        gateway_chat(timeout=60)
    wait_until(lambda: _count_rows() >= 1, timeout=30, desc="rows before restart")
    before = _count_rows()
    registry_before = int(pg_query("SELECT COUNT(*) FROM model_registry", fetch="one"))

    # Restart the stateless services only — volumes must keep the data
    compose("restart", "gateway", "ui", timeout=300)
    # Wait for both to come back healthy (healthcheck needs a few cycles)
    wait_until(lambda: service_health("gateway") == "healthy",
               timeout=180, interval=5.0, desc="gateway healthy after restart")
    wait_until(lambda: service_health("ui") == "healthy",
               timeout=180, interval=5.0, desc="ui healthy after restart")

    after = _count_rows()
    assert after >= before, f"lost logs across restart: {before} -> {after}"
    registry_after = int(pg_query("SELECT COUNT(*) FROM model_registry", fetch="one"))
    assert registry_after == registry_before


def test_registry_survives_full_down_up_with_volumes():
    """docker compose down + up (volumes preserved) keeps all data."""
    registry_before = int(pg_query("SELECT COUNT(*) FROM model_registry", fetch="one"))
    assert registry_before > 0

    compose("down", timeout=180)
    compose("up", "-d", timeout=600)

    for svc in ("postgres", "redis", "gateway", "adapter", "ui", "mock-provider"):
        wait_service_healthy(svc, timeout=120)

    registry_after = int(pg_query("SELECT COUNT(*) FROM model_registry", fetch="one"))
    assert registry_after == registry_before, \
        f"registry lost data across down/up: {registry_before} -> {registry_after}"

    # Gateway serves again after full cycle
    status, body = gateway_chat(timeout=90)
    assert status == 200, body
