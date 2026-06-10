from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PublishAttempt(Base):
    """Publishing result for one generated clip and platform."""

    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clip_record_id: Mapped[int] = mapped_column(ForeignKey("clip_records.id"), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ScheduleSlot(Base):
    """Enabled daily or weekly Scheduled Source URL slot."""

    __tablename__ = "schedule_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False)
    weekday: Mapped[str | None] = mapped_column(String(16), nullable=True)
    local_time: Mapped[str] = mapped_column(String(5), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class RunLog(Base):
    """Persistent record of a Bot Control Mode clipping run."""

    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    selected_highlights: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    events: Mapped[list["RunEvent"]] = relationship(back_populates="run")
    highlight_candidates: Mapped[list["HighlightCandidate"]] = relationship(back_populates="run")


class RunEvent(Base):
    """Progress or error event for a Run Log."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run_logs.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    run: Mapped[RunLog] = relationship(back_populates="events")


class HighlightCandidate(Base):
    """Highlight candidate shown for Manual Clipping review."""

    __tablename__ = "highlight_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run_logs.id"), nullable=False, index=True)
    candidate_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[str] = mapped_column(String(32), nullable=False)
    end_time: Mapped[str] = mapped_column(String(32), nullable=False)
    virality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    hook_text: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[RunLog] = relationship(back_populates="highlight_candidates")


class SourceVideo(Base):
    """A YouTube URL submitted to the Source Video Queue."""

    __tablename__ = "source_videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClipRecord(Base):
    """Generated clip stored in the Clip Archive."""

    __tablename__ = "clip_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clip_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    archive_path: Mapped[str] = mapped_column(Text, nullable=False)
    public_clip_link: Mapped[str] = mapped_column(Text, nullable=False)
    generated_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_hashtags: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


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
