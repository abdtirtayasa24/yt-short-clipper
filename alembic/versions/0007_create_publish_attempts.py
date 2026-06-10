"""create publish attempts

Revision ID: 0007_create_publish_attempts
Revises: 0006_create_schedule_slots
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_create_publish_attempts"
down_revision: str | None = "0006_create_schedule_slots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publish_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clip_record_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("platform_url", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["clip_record_id"], ["clip_records.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_publish_attempts_clip_record_id"), "publish_attempts", ["clip_record_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_publish_attempts_clip_record_id"), table_name="publish_attempts")
    op.drop_table("publish_attempts")
