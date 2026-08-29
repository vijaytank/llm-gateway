"""003_add_provider_credentials

Add provider_credentials table for encrypted provider API key storage (P1.2.1).

Revision ID: 003
Revises: 002
Create Date: 2026-08-27
"""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("api_key_encrypted", sa.String(500), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("provider_name"),
    )
    op.create_index(
        "ix_provider_credentials_provider_name",
        "provider_credentials",
        ["provider_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_credentials_provider_name", table_name="provider_credentials")
    op.drop_table("provider_credentials")
