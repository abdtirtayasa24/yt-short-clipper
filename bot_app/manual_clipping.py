import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy.orm import Session

from bot_app.ai_providers import GeminiTextProvider
from bot_app.database import ensure_workflow_defaults
from bot_app.models import HighlightCandidate, RunEvent, RunLog
from bot_app.settings import Settings


@dataclass
class HighlightDraft:
    title: str
    start_time: str
    end_time: str
    virality_score: int
    hook_text: str
    description: str


class HighlightFinder(Protocol):
    def find_highlights(self, youtube_url: str, count: int) -> list[HighlightDraft]:
        ...


class GeminiHighlightFinder:
    def __init__(self, settings: Settings):
        self.provider = GeminiTextProvider(settings)

    def find_highlights(self, youtube_url: str, count: int) -> list[HighlightDraft]:
        prompt = (
            f"Find {count} short-form highlight candidates for this YouTube URL: {youtube_url}. "
            "Return JSON array with title, start_time, end_time, virality_score, hook_text, description."
        )
        raw_response = self.provider.generate_text(prompt)
        data = json.loads(raw_response)
        return [HighlightDraft(**item) for item in data[:count]]


class ManualClippingService:
    def __init__(self, settings: Settings, highlight_finder: HighlightFinder | None = None):
        self.settings = settings
        self.highlight_finder = highlight_finder or GeminiHighlightFinder(settings)

    def start_run(self, session: Session, youtube_url: str) -> RunLog:
        defaults = ensure_workflow_defaults(session)
        run = RunLog(source_url=youtube_url, status="finding_highlights")
        session.add(run)
        session.commit()
        session.refresh(run)
        self.add_event(session, run, "started", "Manual Clipping started")

        try:
            drafts = self.highlight_finder.find_highlights(youtube_url, defaults.manual_highlight_candidates)
            for index, draft in enumerate(drafts, start=1):
                session.add(
                    HighlightCandidate(
                        run_id=run.id,
                        candidate_number=index,
                        title=draft.title,
                        start_time=draft.start_time,
                        end_time=draft.end_time,
                        virality_score=draft.virality_score,
                        hook_text=draft.hook_text,
                        description=draft.description,
                    )
                )
            run.status = "awaiting_selection"
            self.add_event(session, run, "highlights_found", f"Found {len(drafts)} highlight candidates")
            session.commit()
            session.refresh(run)
            return run
        except Exception as exc:
            run.status = "failed"
            run.error_message = str(exc)
            self.add_event(session, run, "error", str(exc))
            session.commit()
            raise

    def select_candidates(self, session: Session, run_id: int, numbers: list[int]) -> bool:
        run = session.get(RunLog, run_id)
        if run is None or run.status != "awaiting_selection":
            return False
        selected = set(numbers)
        for candidate in run.highlight_candidates:
            candidate.selected = candidate.candidate_number in selected
        run.status = "selection_ready"
        run.selected_highlights = ",".join(str(number) for number in numbers)
        self.add_event(session, run, "selected", f"Selected highlights: {run.selected_highlights}")
        session.commit()
        return True

    def cancel_run(self, session: Session, run_id: int) -> bool:
        run = session.get(RunLog, run_id)
        if run is None or run.status not in {"finding_highlights", "awaiting_selection"}:
            return False
        run.status = "cancelled"
        self.add_event(session, run, "cancelled", "Manual Clipping cancelled")
        session.commit()
        return True

    def add_event(self, session: Session, run: RunLog, event_type: str, message: str) -> None:
        session.add(RunEvent(run_id=run.id, event_type=event_type, message=message, created_at=datetime.now(timezone.utc)))
