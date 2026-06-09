"""create manual clipping runs

Revision ID: 0004_create_manual_clipping_runs
Revises: 0003_create_source_videos
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_create_manual_clipping_runs"
down_revision: str | None = "0003_create_source_videos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("selected_highlights", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_logs_status"), "run_logs", ["status"], unique=False)
    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["run_logs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_events_run_id"), "run_events", ["run_id"], unique=False)
    op.create_table(
        "highlight_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("candidate_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("start_time", sa.String(length=32), nullable=False),
        sa.Column("end_time", sa.String(length=32), nullable=False),
        sa.Column("virality_score", sa.Integer(), nullable=False),
        sa.Column("hook_text", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["run_logs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_highlight_candidates_run_id"), "highlight_candidates", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_highlight_candidates_run_id"), table_name="highlight_candidates")
    op.drop_table("highlight_candidates")
    op.drop_index(op.f("ix_run_events_run_id"), table_name="run_events")
    op.drop_table("run_events")
    op.drop_index(op.f("ix_run_logs_status"), table_name="run_logs")
    op.drop_table("run_logs")
