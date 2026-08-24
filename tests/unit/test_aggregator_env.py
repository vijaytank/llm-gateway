"""Unit tests for brain/aggregator.py and gateway/env_security.py — F-M15."""

from datetime import datetime, timedelta, timezone

import pytest

from schemas.db import RequestLog
from brain.aggregator import aggregate_hour, aggregate_day, run_aggregation
from schemas.db import ModelStatsHourly


# ---------------------------------------------------------------------------
# aggregator — pure SQL logic via SQLite in-memory (dialect-portable schema)
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from schemas.db import Base, RequestLog, ModelRegistry  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = Session(engine)
    yield s
    s.close()
    engine.dispose()


def _mk_log(ts, model="m1", provider="prov", status="success", err=None, lat=100):
    import uuid
    return RequestLog(
        id=uuid.uuid4(), timestamp=ts, virtual_model="auto-free",
        actual_model=model, provider=provider, status=status,
        error_code=err, latency_ms=lat, input_tokens=10, output_tokens=5,
        request_metadata={}, response_metadata={},
    )


def test_aggregate_hour_counts(session):
    base = datetime(2025, 8, 1, 10, 0, tzinfo=timezone.utc)
    session.add_all([
        _mk_log(base), _mk_log(base.replace(minute=30)),
        _mk_log(base.replace(minute=45), status="error", err="429", lat=300),
        _mk_log(base - timedelta(hours=2)),  # outside bucket — ignored
    ])
    session.flush()
    n = aggregate_hour(session, base)
    rows = session.query(ModelStatsHourly).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.request_count == 3
    assert row.error_count == 1
    assert row.error_429_count == 1
    assert row.avg_latency_ms == pytest.approx((100 + 100 + 300) / 3)


def test_aggregate_hour_rerun_updates_not_duplicates(session):
    base = datetime(2025, 8, 1, 10, 0, tzinfo=timezone.utc)
    session.add(_mk_log(base))
    session.flush()
    aggregate_hour(session, base)
    aggregate_hour(session, base)  # idempotent upsert path
    assert session.query(ModelStatsHourly).count() == 1


def test_run_aggregation_smoke(session, monkeypatch):
    """Full tick runs against a real engine; daily rollup fires on 23:00."""
    now = datetime.now(timezone.utc)
    # Craft "now" so prev_hour is 23:00 → daily rollup branch executes.
    if now.hour != 0:
        target = now.replace(hour=23, minute=30, second=0, microsecond=0)
        if target > now:
            target -= timedelta(days=1)
        fake_dt = type("DT", (), {"now": staticmethod(lambda tz=None: target)})
        monkeypatch.setattr("brain.aggregator.datetime", fake_dt)

    class FakeEngineHolder:
        pass

    # Patch _engine to return an in-memory engine wired to our schema
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from schemas.db import Base
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def fake_engine(dsn=None):
        return engine

    import brain.aggregator as agg
    monkeypatch.setattr(agg, "_engine", fake_engine)
    result = agg.run_aggregation()
    assert "hourly_rows" in result and "request_logs_deleted" in result


# ---------------------------------------------------------------------------
# env_security
# ---------------------------------------------------------------------------

def test_env_security_missing_file(tmp_path):
    from gateway.env_security import check_env_permissions
    assert check_env_permissions(str(tmp_path / "nope.env")) is True


def test_env_security_windows_noop(tmp_path, monkeypatch):
    """On Windows the check is skipped entirely (documented Issue-12 limit)."""
    import sys
    from gateway.env_security import check_env_permissions
    env = tmp_path / ".env"
    env.write_text("K=v\n")
    if sys.platform == "win32":
        assert check_env_permissions(str(env)) is True
