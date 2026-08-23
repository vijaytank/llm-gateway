"""Phase 2 unit tests: brain/aggregator.py (plan test_aggregator.py).

Given N test rows in request_logs, hourly aggregation writes correct
request_count / error_count / avg_latency_ms to model_stats_hourly.
Runs on SQLite via SQLAlchemy (dialect-portable models) — no live Postgres.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from schemas.db import Base, ModelStatsHourly, RequestLog
from brain.aggregator import aggregate_day, aggregate_hour, apply_retention


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _log(model, provider, status="success", error_code=None,
         latency=100, ts=None):
    return RequestLog(
        virtual_model="auto-free",
        actual_model=model,
        provider=provider,
        status=status,
        error_code=error_code,
        latency_ms=latency,
        timestamp=ts or datetime.now(timezone.utc),
        request_metadata={}, response_metadata={},
    )


def test_hourly_aggregation_counts(session):
    hour = datetime(2026, 8, 22, 10, tzinfo=timezone.utc)
    rows = []
    for i in range(7):  # 7 successes at 200ms
        rows.append(_log("nvidia-auto", "nvidia", latency=200, ts=hour + timedelta(minutes=i)))
    for i in range(3):  # 3 failures
        rows.append(_log("nvidia-auto", "nvidia", status="error",
                         error_code="429", latency=400,
                         ts=hour + timedelta(minutes=30 + i)))
    session.add_all(rows)
    session.flush()

    written = aggregate_hour(session, hour)
    session.commit()

    stat = session.execute(
        select(ModelStatsHourly).where(ModelStatsHourly.model_name == "nvidia-auto")
    ).scalar_one()

    assert written == 1
    assert stat.request_count == 10
    assert stat.error_count == 3
    assert stat.error_429_count == 3
    assert stat.error_5xx_count == 0
    assert stat.avg_latency_ms == pytest.approx((7 * 200 + 3 * 400) / 10)


def test_hourly_aggregation_groups_by_model(session):
    hour = datetime(2026, 8, 22, 11, tzinfo=timezone.utc)
    session.add_all([
        _log("a", "p1", ts=hour),
        _log("b", "p2", ts=hour),
    ])
    aggregate_hour(session, hour)
    stats = session.execute(select(ModelStatsHourly)).scalars().all()
    assert {(s.model_name, s.provider) for s in stats} == {("a", "p1"), ("b", "p2")}


def test_daily_aggregation_rates(session):
    day = datetime(2026, 8, 22, tzinfo=timezone.utc)
    h1 = day.replace(hour=1)
    h2 = day.replace(hour=2)
    session.add_all([
        ModelStatsHourly(model_name="m", provider="p", hour_bucket=h1,
                         request_count=6, error_count=2, error_429_count=1,
                         error_5xx_count=1, avg_latency_ms=150.0, avg_tokens=20.0),
        ModelStatsHourly(model_name="m", provider="p", hour_bucket=h2,
                         request_count=4, error_count=0, error_429_count=0,
                         error_5xx_count=0, avg_latency_ms=250.0, avg_tokens=40.0),
    ])
    session.flush()
    n = aggregate_day(session, day)
    assert n == 1

    from schemas.db import ModelStatsDaily
    daily = session.execute(select(ModelStatsDaily)).scalar_one()
    assert daily.request_count == 10
    assert daily.error_count == 2
    assert daily.success_rate == pytest.approx(0.8)
    assert daily.rate_429_rate == pytest.approx(0.1)


def test_retention_deletes_old_rows(session):
    now = datetime.now(timezone.utc)
    session.add_all([
        _log("old", "p", ts=now - timedelta(days=12)),
        _log("fresh", "p", ts=now - timedelta(days=1)),
        ModelStatsHourly(model_name="old-h", provider="p",
                         hour_bucket=now - timedelta(days=45),
                         request_count=1, error_count=0, error_429_count=0,
                         error_5xx_count=0),
        ModelStatsHourly(model_name="new-h", provider="p",
                         hour_bucket=now - timedelta(days=2),
                         request_count=1, error_count=0, error_429_count=0,
                         error_5xx_count=0),
    ])
    session.flush()
    result = apply_retention(session)
    session.commit()

    assert result["request_logs_deleted"] == 1
    assert result["hourly_stats_deleted"] == 1
    remaining = session.execute(select(RequestLog.actual_model)).scalars().all()
    assert remaining == ["fresh"]
