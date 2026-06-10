"""add run cancellation

Revision ID: 0008_add_run_cancellation
Revises: 0007_create_publish_attempts
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_add_run_cancellation"
down_revision: str | None = "0007_create_publish_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_logs",
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("run_logs", "cancellation_requested")
