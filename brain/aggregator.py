"""
brain/aggregator.py — Hourly aggregation job (Phase 2 deliverable 7)

Runs every hour via APScheduler in brain/main.py. Per master plan:
- Reads request_logs from Postgres for the previous hour.
- Writes to model_stats_hourly.
- Deletes request_logs rows older than 10 days.
- Writes to model_stats_daily (midnight-only aggregation).
- Deletes model_stats_hourly rows older than 30 days.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import Session

from schemas.db import (
    ModelStatsDaily,
    ModelStatsHourly,
    RequestLog,
)

REQUEST_LOG_RETENTION_DAYS = 10
HOURLY_STATS_RETENTION_DAYS = 30


def _engine(dsn: str | None = None):
    dsn = dsn or os.environ.get("GATEWAY_DB_URL") or os.environ.get("DATABASE_URL")
    return create_engine(dsn)


def aggregate_hour(session: Session, hour_bucket: datetime) -> int:
    """Aggregate request_logs for a single hour bucket into model_stats_hourly.

    Returns the number of rows written.
    """
    window_start = hour_bucket.replace(minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=1)

    rows = session.execute(
        select(
            RequestLog.actual_model,
            RequestLog.provider,
            func.count().label("request_count"),
            func.sum(text("CASE WHEN status != 'success' THEN 1 ELSE 0 END")).label("error_count"),
            func.sum(text("CASE WHEN error_code = '429' THEN 1 ELSE 0 END")).label("error_429_count"),
            func.sum(text("CASE WHEN error_code IN ('500','502','503','504') THEN 1 ELSE 0 END")).label("error_5xx_count"),
            func.avg(RequestLog.latency_ms),
            func.avg(func.coalesce(RequestLog.input_tokens, 0) + func.coalesce(RequestLog.output_tokens, 0)),
        )
        .where(RequestLog.timestamp >= window_start, RequestLog.timestamp < window_end)
        .group_by(RequestLog.actual_model, RequestLog.provider)
    ).all()

    for actual_model, provider, req_count, err_count, err_429, err_5xx, avg_latency, avg_tokens in rows:
        if not actual_model:
            continue
        existing = session.execute(
            select(ModelStatsHourly).where(
                ModelStatsHourly.model_name == actual_model,
                ModelStatsHourly.provider == provider,
                ModelStatsHourly.hour_bucket == window_start,
            )
        ).scalar_one_or_none()
        if existing:
            existing.request_count = req_count
            existing.error_count = int(err_count or 0)
            existing.error_429_count = int(err_429 or 0)
            existing.error_5xx_count = int(err_5xx or 0)
            existing.avg_latency_ms = float(avg_latency or 0)
            existing.avg_tokens = float(avg_tokens or 0)
        else:
            session.add(ModelStatsHourly(
                model_name=actual_model,
                provider=provider,
                hour_bucket=window_start,
                request_count=req_count,
                error_count=int(err_count or 0),
                error_429_count=int(err_429 or 0),
                error_5xx_count=int(err_5xx or 0),
                avg_latency_ms=float(avg_latency or 0),
                avg_tokens=float(avg_tokens or 0),
            ))
    session.flush()
    return len(rows)


def aggregate_day(session: Session, day_bucket: datetime) -> int:
    """Aggregate model_stats_hourly rows for a day into model_stats_daily."""
    day_start = day_bucket.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    rows = session.execute(
        select(
            ModelStatsHourly.model_name,
            ModelStatsHourly.provider,
            func.sum(ModelStatsHourly.request_count).label("request_count"),
            func.sum(ModelStatsHourly.error_count).label("error_count"),
            func.sum(ModelStatsHourly.error_429_count).label("error_429_count"),
            func.sum(ModelStatsHourly.error_5xx_count).label("error_5xx_count"),
            func.avg(ModelStatsHourly.avg_latency_ms).label("avg_latency_ms"),
            func.avg(ModelStatsHourly.avg_tokens).label("avg_tokens"),
        )
        .where(ModelStatsHourly.hour_bucket >= day_start, ModelStatsHourly.hour_bucket < day_end)
        .group_by(ModelStatsHourly.model_name, ModelStatsHourly.provider)
    ).all()

    for model_name, provider, req_count, err_count, e429, e5xx, avg_lat, avg_tok in rows:
        total = int(req_count or 0)
        errors = int(err_count or 0)
        session.merge(ModelStatsDaily(
            model_name=model_name,
            provider=provider,
            day_bucket=day_start,
            request_count=total,
            error_count=errors,
            error_429_count=int(e429 or 0),
            error_5xx_count=int(e5xx or 0),
            avg_latency_ms=float(avg_lat or 0),
            avg_tokens=float(avg_tok or 0),
            success_rate=(total - errors) / total if total else None,
            rate_429_rate=(int(e429 or 0) / total) if total else None,
            rate_5xx_rate=(int(e5xx or 0) / total) if total else None,
        ))
    session.flush()
    return len(rows)


def apply_retention(session: Session) -> dict[str, int]:
    """Delete expired request_logs (>10 days) and hourly stats (>30 days)."""
    cutoff_requests = datetime.now(timezone.utc) - timedelta(days=REQUEST_LOG_RETENTION_DAYS)
    cutoff_hourly = datetime.now(timezone.utc) - timedelta(days=HOURLY_STATS_RETENTION_DAYS)

    deleted_requests = session.execute(
        delete(RequestLog).where(RequestLog.timestamp < cutoff_requests)
    ).rowcount
    deleted_hourly = session.execute(
        delete(ModelStatsHourly).where(ModelStatsHourly.hour_bucket < cutoff_hourly)
    ).rowcount
    return {"request_logs_deleted": deleted_requests, "hourly_stats_deleted": deleted_hourly}


def run_aggregation(dsn: str | None = None) -> dict:
    """One aggregation tick: previous full hour + midnight daily rollup + retention."""
    now = datetime.now(timezone.utc)
    prev_hour = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    engine = _engine(dsn)
    with Session(engine) as session:
        hourly_rows = aggregate_hour(session, prev_hour)
        # Daily rollup only at midnight UTC (covers the finished previous day)
        written_daily = 0
        if now.hour == 0 and now.minute < 5:
            written_daily = aggregate_day(session, now - timedelta(days=1))
        retention = apply_retention(session)
        session.commit()
    return {"prev_hour": prev_hour.isoformat(), "hourly_rows": hourly_rows,
            "daily_rows": written_daily, **retention}


if __name__ == "__main__":
    import json
    print(json.dumps(run_aggregation(), default=str, indent=2))
