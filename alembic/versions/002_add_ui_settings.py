"""002_add_ui_settings

Add ui_settings table for UI admin password storage (Phase 4).

Revision ID: 002
Revises: 001
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("ui_settings")
