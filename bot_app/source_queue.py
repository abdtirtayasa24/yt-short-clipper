from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from bot_app.models import SourceVideo

PENDING = "pending"
CONSUMED = "consumed"
FAILED = "failed"
CANCELLED = "cancelled"


def add_source_videos(session: Session, urls: list[str]) -> list[SourceVideo]:
    sources = [SourceVideo(url=url, status=PENDING) for url in urls]
    session.add_all(sources)
    session.commit()
    for source in sources:
        session.refresh(source)
    return sources


def get_source_videos(session: Session) -> list[SourceVideo]:
    return list(session.scalars(select(SourceVideo).order_by(SourceVideo.id)).all())


def get_pending_source_videos(session: Session, limit: int | None = None) -> list[SourceVideo]:
    statement = select(SourceVideo).where(SourceVideo.status == PENDING).order_by(SourceVideo.id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement).all())


def cancel_pending_source_video(session: Session, source_id: int) -> bool:
    source = session.get(SourceVideo, source_id)
    if source is None or source.status != PENDING:
        return False
    source.status = CANCELLED
    session.commit()
    return True


def consume_source_video(session: Session, source: SourceVideo) -> SourceVideo:
    source.status = CONSUMED
    source.consumed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(source)
    return source
