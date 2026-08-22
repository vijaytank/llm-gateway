"""
SQLAlchemy ORM models for the LLM Gateway database schema.

These models define the canonical database schema used by:
- Gateway (writes request_logs)
- Brain (reads/writes model_registry, writes model_stats_*)
- Wizard/UI (reads/writes model_registry)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ModelRegistry(Base):
    """Registry of all known models across all providers."""

    __tablename__ = "model_registry"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="free")  # free, premium
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    probe_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="builtin")  # builtin, custom
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Quota limits (from llm-rate-limits-tracker)
    rpm: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # requests per minute
    tpm: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # tokens per minute
    rph: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # requests per hour
    tph: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # tokens per hour
    rpd: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # requests per day
    tpd: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # tokens per day

    # Extra metadata
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("provider", "model_name", name="uq_model_registry_provider_model"),
        Index("ix_model_registry_tier_enabled", "tier", "enabled"),
    )

    def __repr__(self) -> str:
        return f"<ModelRegistry(provider={self.provider}, model={self.model_name}, tier={self.tier})>"


class RequestLog(Base):
    """Per-request metadata log for routing decisions, debugging, and analytics."""

    __tablename__ = "request_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Client identification
    client_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    virtual_model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # Routing outcome
    actual_model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Result
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # success, error, fallback
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # rate_limit, server_error, auth_error, etc.

    # Token usage
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Latency
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ttft_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # time to first token

    # Routing decision metadata
    routing_decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Full request/response metadata (JSON for flexibility)
    request_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    response_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_request_logs_timestamp_status", "timestamp", "status"),
        Index("ix_request_logs_virtual_model_timestamp", "virtual_model", "timestamp"),
        Index("ix_request_logs_provider_timestamp", "provider", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<RequestLog(id={self.id}, model={self.virtual_model}, status={self.status})>"


class ModelStatsHourly(Base):
    """Hourly aggregated statistics per model/provider."""

    __tablename__ = "model_stats_hourly"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_429_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_5xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    avg_latency_ms: Mapped[Optional[float]] = mapped_column(nullable=True)
    avg_tokens: Mapped[Optional[float]] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("model_name", "provider", "hour_bucket", name="uq_model_stats_hourly"),
        Index("ix_model_stats_hourly_provider_bucket", "provider", "hour_bucket"),
    )

    def __repr__(self) -> str:
        return f"<ModelStatsHourly(model={self.model_name}, provider={self.provider}, hour={self.hour_bucket})>"


class ModelStatsDaily(Base):
    """Daily aggregated statistics per model/provider."""

    __tablename__ = "model_stats_daily"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    day_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_429_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_5xx_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    success_rate: Mapped[Optional[float]] = mapped_column(nullable=True)
    rate_429_rate: Mapped[Optional[float]] = mapped_column(nullable=True)
    rate_5xx_rate: Mapped[Optional[float]] = mapped_column(nullable=True)
    avg_latency_ms: Mapped[Optional[float]] = mapped_column(nullable=True)
    avg_tokens: Mapped[Optional[float]] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("model_name", "provider", "day_bucket", name="uq_model_stats_daily"),
        Index("ix_model_stats_daily_provider_bucket", "provider", "day_bucket"),
    )

    def __repr__(self) -> str:
        return f"<ModelStatsDaily(model={self.model_name}, provider={self.provider}, day={self.day_bucket})>"