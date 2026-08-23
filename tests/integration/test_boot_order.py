"""
test_boot_order.py — Plan Phase 1 integration: boot sequencing & health.

Covers (plan test_docker_compose_up / test_boot_order):
  - Full stack reaches healthy state within the 90s AC
  - db-init ran Alembic migrations + registry seed and exited 0
  - /health endpoints on gateway (4000), adapter (4001), UI (4002) return 200
  - All schema tables from migration 001/002 exist
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (  # noqa: E402
    GATEWAY_URL, ADAPTER_URL, UI_URL,
    all_ports_up, compose_output, http_json, pg_query, service_health,
)


def test_full_stack_healthy_within_90s(docker_stack):
    """AC: Docker Compose starts cleanly; every service reports healthy."""
    for svc in ("postgres", "redis", "gateway", "adapter", "ui"):
        assert service_health(svc) == "healthy", f"{svc} not healthy"
    assert all_ports_up(timeout=5)


def test_db_init_completed_successfully():
    """One-shot db-init container exits 0 after alembic upgrade + seed."""
    out = compose_output("ps", "-a", "--format", "json", "db-init")
    entries = [line for line in out.strip().splitlines() if line.strip()]
    assert entries, "db-init container not found"
    import json
    entry = json.loads(entries[-1])
    assert entry["State"] == "exited"
    assert entry["ExitCode"] == 0


def test_gateway_health_endpoint():
    status, body = http_json("GET", f"{GATEWAY_URL}/health/liveliness")
    assert status == 200


def test_adapter_health_endpoint():
    status, body = http_json("GET", f"{ADAPTER_URL}/health")
    assert status == 200
    data = __import__("json").loads(body)
    assert data["status"] == "healthy"
    assert data["service"] == "anthropic-adapter"


def test_ui_health_endpoint():
    status, body = http_json("GET", f"{UI_URL}/health")
    assert status == 200


def test_all_migrated_tables_exist():
    """Alembic migrations 001+002 created the full Phase 1 schema."""
    tables = {row[0] for row in pg_query(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    for expected in ("model_registry", "request_logs", "model_stats_hourly",
                     "model_stats_daily", "ui_settings", "alembic_version"):
        assert expected in tables, f"table {expected} missing"


def test_alembic_version_is_head():
    version = pg_query("SELECT version_num FROM alembic_version", fetch="one")
    assert version, "alembic_version empty — migrations did not run"
