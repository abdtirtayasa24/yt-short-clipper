from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bot_app.database import ensure_workflow_defaults
from bot_app.manual_clipping import ManualClippingService
from bot_app.models import RunEvent, RunLog, ScheduleSlot
from bot_app.settings import Settings
from bot_app.source_queue import consume_source_video, get_pending_source_videos

WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


@dataclass
class ScheduledFireResult:
    run_id: int
    source_url: str | None
    message_telegram: bool


def is_valid_time(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return False
    hour, minute = [int(part) for part in parts]
    return 0 <= hour <= 23 and 0 <= minute <= 59


def add_daily_schedule(session: Session, settings: Settings, local_time: str) -> ScheduleSlot | None:
    if not is_valid_time(local_time):
        return None
    slot = ScheduleSlot(cadence="daily", local_time=local_time, timezone=settings.app_timezone, enabled=True)
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot


def add_weekly_schedule(session: Session, settings: Settings, weekday: str, local_time: str) -> ScheduleSlot | None:
    normalized_weekday = weekday.lower()
    if normalized_weekday not in WEEKDAYS or not is_valid_time(local_time):
        return None
    slot = ScheduleSlot(
        cadence="weekly",
        weekday=normalized_weekday,
        local_time=local_time,
        timezone=settings.app_timezone,
        enabled=True,
    )
    session.add(slot)
    session.commit()
    session.refresh(slot)
    return slot


def list_schedules(session: Session) -> list[ScheduleSlot]:
    return list(session.scalars(select(ScheduleSlot).order_by(ScheduleSlot.id)).all())


def set_schedule_enabled(session: Session, schedule_id: int, enabled: bool) -> bool:
    slot = session.get(ScheduleSlot, schedule_id)
    if slot is None:
        return False
    slot.enabled = enabled
    session.commit()
    return True


def remove_schedule(session: Session, schedule_id: int) -> bool:
    return set_schedule_enabled(session, schedule_id, False)


def delete_schedule(session: Session, schedule_id: int) -> bool:
    slot = session.get(ScheduleSlot, schedule_id)
    if slot is None:
        return False
    session.delete(slot)
    session.commit()
    return True


def fire_schedule(
    session: Session,
    settings: Settings,
    schedule_id: int,
    manual_clipping_service: ManualClippingService | None = None,
) -> ScheduledFireResult:
    defaults = ensure_workflow_defaults(session)
    pending_sources = get_pending_source_videos(session, limit=defaults.scheduled_source_videos_per_run)
    run = RunLog(source_url="", status="scheduled_empty")
    session.add(run)
    session.commit()
    session.refresh(run)

    if not pending_sources:
        session.add(RunEvent(run_id=run.id, event_type="scheduled_empty", message=f"Schedule {schedule_id} fired with empty Source Video Queue"))
        session.commit()
        return ScheduledFireResult(run_id=run.id, source_url=None, message_telegram=False)

    source = pending_sources[0]
    run.source_url = source.url
    run.status = "scheduled_source_consumed"
    session.add(RunEvent(run_id=run.id, event_type="source_consumed", message=source.url))
    consume_source_video(session, source)
    session.commit()

    if manual_clipping_service is not None:
        manual_clipping_service.start_run(session, source.url)

    return ScheduledFireResult(run_id=run.id, source_url=source.url, message_telegram=True)
