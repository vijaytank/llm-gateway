"""001_initial_schema

Create all tables for the LLM Gateway.

Revision ID: 001
Revises:
Create Date: 2025-08-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # model_registry
    op.create_table(
        "model_registry",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False, server_default="free"),
        sa.Column("capabilities", JSONB, nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("probe_timeout_seconds", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("source", sa.String(32), nullable=False, server_default="builtin"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("rpm", sa.Integer(), nullable=True),
        sa.Column("tpm", sa.Integer(), nullable=True),
        sa.Column("rph", sa.Integer(), nullable=True),
        sa.Column("tph", sa.Integer(), nullable=True),
        sa.Column("rpd", sa.Integer(), nullable=True),
        sa.Column("tpd", sa.Integer(), nullable=True),
        sa.Column("extra", JSONB, nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id", name="pk_model_registry"),
    )
    op.create_unique_constraint("uq_model_registry_provider_model", "model_registry", ["provider", "model_name"])
    op.create_index("ix_model_registry_tier_enabled", "model_registry", ["tier", "enabled"])
    op.create_index("ix_model_registry_provider", "model_registry", ["provider"])

    # request_logs
    op.create_table(
        "request_logs",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=True),
        sa.Column("virtual_model", sa.String(128), nullable=False),
        sa.Column("actual_model", sa.String(256), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("routing_decision_reason", sa.Text(), nullable=True),
        sa.Column("request_metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("response_metadata", JSONB, nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id", name="pk_request_logs"),
    )
    op.create_index("ix_request_logs_timestamp", "request_logs", ["timestamp"])
    op.create_index("ix_request_logs_timestamp_status", "request_logs", ["timestamp", "status"])
    op.create_index("ix_request_logs_virtual_model_timestamp", "request_logs", ["virtual_model", "timestamp"])
    op.create_index("ix_request_logs_provider_timestamp", "request_logs", ["provider", "timestamp"])
    op.create_index("ix_request_logs_client_id", "request_logs", ["client_id"])
    op.create_index("ix_request_logs_status", "request_logs", ["status"])

    # model_stats_hourly
    op.create_table(
        "model_stats_hourly",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_429_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_5xx_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("avg_latency_ms", sa.Float(), nullable=True),
        sa.Column("avg_tokens", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_model_stats_hourly"),
    )
    op.create_unique_constraint("uq_model_stats_hourly", "model_stats_hourly", ["model_name", "provider", "hour_bucket"])
    op.create_index("ix_model_stats_hourly_provider_bucket", "model_stats_hourly", ["provider", "hour_bucket"])
    op.create_index("ix_model_stats_hourly_model_bucket", "model_stats_hourly", ["model_name", "hour_bucket"])

    # model_stats_daily
    op.create_table(
        "model_stats_daily",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("day_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_429_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_5xx_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("success_rate", sa.Float(), nullable=True),
        sa.Column("rate_429_rate", sa.Float(), nullable=True),
        sa.Column("rate_5xx_rate", sa.Float(), nullable=True),
        sa.Column("avg_latency_ms", sa.Float(), nullable=True),
        sa.Column("avg_tokens", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_model_stats_daily"),
    )
    op.create_unique_constraint("uq_model_stats_daily", "model_stats_daily", ["model_name", "provider", "day_bucket"])
    op.create_index("ix_model_stats_daily_provider_bucket", "model_stats_daily", ["provider", "day_bucket"])
    op.create_index("ix_model_stats_daily_model_bucket", "model_stats_daily", ["model_name", "day_bucket"])


def downgrade() -> None:
    op.drop_table("model_stats_daily")
    op.drop_table("model_stats_hourly")
    op.drop_table("request_logs")
    op.drop_table("model_registry")