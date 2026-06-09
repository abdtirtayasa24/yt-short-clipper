"""create workflow defaults

Revision ID: 0001_create_workflow_defaults
Revises: 
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_create_workflow_defaults"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_defaults",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("captions_enabled", sa.Boolean(), nullable=False),
        sa.Column("hooks_enabled", sa.Boolean(), nullable=False),
        sa.Column("publish_youtube", sa.Boolean(), nullable=False),
        sa.Column("publish_tiktok", sa.Boolean(), nullable=False),
        sa.Column("subtitle_language", sa.String(length=16), nullable=False),
        sa.Column("manual_highlight_candidates", sa.Integer(), nullable=False),
        sa.Column("scheduled_highlight_candidates", sa.Integer(), nullable=False),
        sa.Column("scheduled_clips_per_source", sa.Integer(), nullable=False),
        sa.Column("scheduled_source_videos_per_run", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("workflow_defaults")
