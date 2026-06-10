from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urljoin

from sqlalchemy import select
from sqlalchemy.orm import Session

from bot_app.models import ClipRecord
from bot_app.settings import Settings


def build_public_clip_link(settings: Settings, clip_id: str) -> str:
    base_url = str(settings.public_base_url).rstrip("/") + "/"
    return urljoin(base_url, f"clips/{clip_id}/download")


def create_clip_record(
    session: Session,
    settings: Settings,
    archive_path: Path,
    *,
    clip_id: str | None = None,
    expires_at: datetime | None = None,
    deleted_at: datetime | None = None,
    generated_title: str | None = None,
    generated_description: str | None = None,
    generated_hashtags: str | None = None,
) -> ClipRecord:
    clip_identifier = clip_id or token_urlsafe(24)
    clip_expires_at = expires_at or datetime.now(timezone.utc) + timedelta(
        days=settings.clip_retention_days,
    )
    record = ClipRecord(
        clip_id=clip_identifier,
        archive_path=str(archive_path),
        public_clip_link=build_public_clip_link(settings, clip_identifier),
        generated_title=generated_title,
        generated_description=generated_description,
        generated_hashtags=generated_hashtags,
        expires_at=clip_expires_at,
        deleted_at=deleted_at,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def cleanup_expired_clips(
    session: Session,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    current_time = _as_aware_utc(now or datetime.now(timezone.utc))
    archive_root = settings.clip_archive_dir.resolve()
    expired_records = session.scalars(
        select(ClipRecord).where(
            ClipRecord.deleted_at.is_(None),
            ClipRecord.expires_at.is_not(None),
            ClipRecord.expires_at <= current_time,
        )
    ).all()

    deleted_count = 0
    for record in expired_records:
        clip_path = Path(record.archive_path).resolve()
        try:
            clip_path.relative_to(archive_root)
        except ValueError:
            continue
        if clip_path.exists():
            clip_path.unlink()
        record.deleted_at = current_time
        deleted_count += 1
    session.commit()
    return deleted_count


def resolve_downloadable_clip_path(
    record: ClipRecord,
    settings: Settings,
    now: datetime | None = None,
) -> Path | None:
    current_time = _as_aware_utc(now or datetime.now(timezone.utc))
    if record.deleted_at is not None:
        return None
    if record.expires_at is not None and _as_aware_utc(record.expires_at) <= current_time:
        return None

    archive_root = settings.clip_archive_dir.resolve()
    clip_path = Path(record.archive_path).resolve()
    try:
        clip_path.relative_to(archive_root)
    except ValueError:
        return None

    if not clip_path.is_file():
        return None
    return clip_path
