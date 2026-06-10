"""add clip metadata

Revision ID: 0005_add_clip_metadata
Revises: 0004_create_manual_clipping_runs
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_add_clip_metadata"
down_revision: str | None = "0004_create_manual_clipping_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clip_records", sa.Column("generated_title", sa.Text(), nullable=True))
    op.add_column("clip_records", sa.Column("generated_description", sa.Text(), nullable=True))
    op.add_column("clip_records", sa.Column("generated_hashtags", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("clip_records", "generated_hashtags")
    op.drop_column("clip_records", "generated_description")
    op.drop_column("clip_records", "generated_title")
