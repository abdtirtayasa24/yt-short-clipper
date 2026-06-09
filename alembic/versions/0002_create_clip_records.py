"""create clip records

Revision ID: 0002_create_clip_records
Revises: 0001_create_workflow_defaults
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_create_clip_records"
down_revision: str | None = "0001_create_workflow_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clip_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clip_id", sa.String(length=64), nullable=False),
        sa.Column("archive_path", sa.Text(), nullable=False),
        sa.Column("public_clip_link", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clip_id"),
    )
    op.create_index(op.f("ix_clip_records_clip_id"), "clip_records", ["clip_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_clip_records_clip_id"), table_name="clip_records")
    op.drop_table("clip_records")
