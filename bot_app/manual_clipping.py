import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from bot_app.ai_providers import GeminiTextProvider
from bot_app.clip_archive import create_clip_record
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


class ClipProcessor(Protocol):
    def process_highlight(
        self,
        source_url: str,
        candidate: HighlightCandidate,
        *,
        captions_enabled: bool,
        hooks_enabled: bool,
    ) -> Path:
        ...


class ExistingClipProcessor:
    def process_highlight(
        self,
        source_url: str,
        candidate: HighlightCandidate,
        *,
        captions_enabled: bool,
        hooks_enabled: bool,
    ) -> Path:
        raise NotImplementedError("Existing clipping behavior is not wired for Bot Control Mode yet")


class ClippingQueue:
    def __init__(self):
        self.active_run_id: int | None = None

    def start(self, run_id: int) -> bool:
        if self.active_run_id is not None:
            return False
        self.active_run_id = run_id
        return True

    def finish(self, run_id: int) -> None:
        if self.active_run_id == run_id:
            self.active_run_id = None


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
    def __init__(
        self,
        settings: Settings,
        highlight_finder: HighlightFinder | None = None,
        clip_processor: ClipProcessor | None = None,
        clipping_queue: ClippingQueue | None = None,
    ):
        self.settings = settings
        self.highlight_finder = highlight_finder or GeminiHighlightFinder(settings)
        self.clip_processor = clip_processor or ExistingClipProcessor()
        self.clipping_queue = clipping_queue or ClippingQueue()

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

    def process_selected_run(self, session: Session, settings: Settings, run_id: int) -> list[str]:
        run = session.get(RunLog, run_id)
        if run is None or run.status != "selection_ready":
            return []
        if not self.clipping_queue.start(run_id):
            run.status = "queued"
            self.add_event(session, run, "queued", "Clipping Queue already has an active run")
            session.commit()
            return []

        defaults = ensure_workflow_defaults(session)
        selected_candidates = [candidate for candidate in run.highlight_candidates if candidate.selected]
        links = []
        try:
            run.status = "processing"
            self.add_event(session, run, "processing", "Processing selected highlights")
            for candidate in selected_candidates:
                output_path = self.clip_processor.process_highlight(
                    run.source_url,
                    candidate,
                    captions_enabled=defaults.captions_enabled,
                    hooks_enabled=defaults.hooks_enabled,
                )
                clip = create_clip_record(session, settings, output_path)
                links.append(clip.public_clip_link)
                self.add_event(session, run, "clip_archived", clip.public_clip_link)
            run.status = "processed"
            self.add_event(session, run, "processed", f"Generated {len(links)} Public Clip Links")
            session.commit()
            return links
        except Exception as exc:
            run.status = "failed"
            run.error_message = str(exc)
            self.add_event(session, run, "error", str(exc))
            session.commit()
            raise
        finally:
            self.clipping_queue.finish(run_id)

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
