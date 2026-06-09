from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkflowDefaults(Base):
    """Singleton Workflow Defaults for Bot Control Mode."""

    __tablename__ = "workflow_defaults"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    captions_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hooks_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    publish_youtube: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    publish_tiktok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subtitle_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    manual_highlight_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    scheduled_highlight_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    scheduled_clips_per_source: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scheduled_source_videos_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
