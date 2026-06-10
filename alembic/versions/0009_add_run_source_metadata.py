"""add run source metadata

Revision ID: 0009_add_run_source_metadata
Revises: 0008_add_run_cancellation
Create Date: 2026-06-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_add_run_source_metadata"
down_revision: str | None = "0008_add_run_cancellation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_logs",
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="youtube"),
    )
    op.add_column("run_logs", sa.Column("source_path", sa.Text(), nullable=True))
    op.add_column("run_logs", sa.Column("source_filename", sa.Text(), nullable=True))
    op.add_column("run_logs", sa.Column("source_content_type", sa.String(length=128), nullable=True))
    op.add_column("run_logs", sa.Column("source_file_size", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_logs", "source_file_size")
    op.drop_column("run_logs", "source_content_type")
    op.drop_column("run_logs", "source_filename")
    op.drop_column("run_logs", "source_path")
    op.drop_column("run_logs", "source_type")
