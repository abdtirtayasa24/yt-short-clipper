"""create schedule slots

Revision ID: 0006_create_schedule_slots
Revises: 0005_add_clip_metadata
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_create_schedule_slots"
down_revision: str | None = "0005_add_clip_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cadence", sa.String(length=16), nullable=False),
        sa.Column("weekday", sa.String(length=16), nullable=True),
        sa.Column("local_time", sa.String(length=5), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("schedule_slots")
