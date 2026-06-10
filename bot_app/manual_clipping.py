import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from sqlalchemy.orm import Session

from bot_app.ai_providers import GeminiTextProvider
from bot_app.clip_archive import create_clip_record
from bot_app.database import ensure_workflow_defaults
from bot_app.models import ClipRecord, HighlightCandidate, PublishAttempt, RunEvent, RunLog
from bot_app.settings import Settings


@dataclass
class PublishingMetadata:
    title: str
    description: str
    hashtags: list[str]


@dataclass
class HighlightDraft:
    title: str
    start_time: str
    end_time: str
    virality_score: int
    hook_text: str
    description: str


class HighlightFinder(Protocol):
    def find_highlights(
        self,
        youtube_url: str,
        count: int,
        subtitle_language: str = "en",
    ) -> list[HighlightDraft]:
        ...


class Publisher(Protocol):
    def publish(self, clip: ClipRecord, metadata: PublishingMetadata) -> str:
        ...


class MetadataGenerator(Protocol):
    def generate_metadata(
        self,
        source_url: str,
        candidate: HighlightCandidate,
        model: str,
    ) -> PublishingMetadata:
        ...


def _normalize_timestamp(value: str) -> str:
    text = str(value).strip().replace(".", ",")
    if ":" not in text:
        return "00:00:00,000"
    parts = text.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        parts = ["00", minutes, seconds]
    if len(parts) != 3:
        return text
    hours, minutes, seconds = parts
    if "," not in seconds:
        seconds = f"{seconds},000"
    whole_seconds, milliseconds = seconds.split(",", 1)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(whole_seconds):02d},{milliseconds[:3].ljust(3, '0')}"


def _timestamp_seconds(value: str) -> float:
    normalized = _normalize_timestamp(value).replace(",", ".")
    hours, minutes, seconds = normalized.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _highlight_draft_from_mapping(item: dict) -> HighlightDraft:
    return HighlightDraft(
        title=str(item.get("title", "Untitled highlight")),
        start_time=_normalize_timestamp(item.get("start_time", "00:00:00,000")),
        end_time=_normalize_timestamp(item.get("end_time", "00:00:30,000")),
        virality_score=int(round(float(item.get("virality_score", 0)))),
        hook_text=str(item.get("hook_text", item.get("title", "Watch this"))),
        description=str(item.get("description", "")),
    )


def _candidate_to_core_highlight(candidate: HighlightCandidate) -> dict:
    start_time = _normalize_timestamp(candidate.start_time)
    end_time = _normalize_timestamp(candidate.end_time)
    return {
        "title": candidate.title,
        "start_time": start_time,
        "end_time": end_time,
        "virality_score": candidate.virality_score,
        "hook_text": candidate.hook_text,
        "description": candidate.description,
        "duration_seconds": max(0.0, _timestamp_seconds(end_time) - _timestamp_seconds(start_time)),
    }


def _parse_gemini_json(raw_response: str, expected_root: str):
    text = (raw_response or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    if expected_root == "array":
        start = text.find("[")
        end = text.rfind("]")
    else:
        start = text.find("{")
        end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Gemini did not return valid JSON. This usually means the model returned prose, "
            "an empty response, or content that was not grounded in a transcript."
        ) from exc


class GeminiMetadataGenerator:
    def __init__(self, settings: Settings):
        self.provider = GeminiTextProvider(settings)

    def generate_metadata(
        self,
        source_url: str,
        candidate: HighlightCandidate,
        model: str,
    ) -> PublishingMetadata:
        prompt = (
            "Generate shared publishing metadata as JSON with title, description, and hashtags "
            f"for this clip from {source_url}: {candidate.title}. {candidate.description}"
        )
        data = _parse_gemini_json(self.provider.generate_text(prompt, model=model), "object")
        return PublishingMetadata(
            title=data["title"],
            description=data["description"],
            hashtags=data.get("hashtags", []),
        )


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
    def __init__(self, settings: Settings):
        self.settings = settings

    def process_highlight(
        self,
        source_url: str,
        candidate: HighlightCandidate,
        *,
        captions_enabled: bool,
        hooks_enabled: bool,
    ) -> Path:
        from clipper_core import AutoClipperCore

        output_dir = self.settings.clip_archive_dir / "processed"
        output_dir.mkdir(parents=True, exist_ok=True)
        client = OpenAI(
            api_key=self.settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        core = AutoClipperCore(
            client=client,
            output_dir=str(output_dir),
            ai_providers={
                "highlight_finder": {
                    "api_key": self.settings.openrouter_api_key,
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": self.settings.openrouter_transcription_model,
                },
                "caption_maker": {
                    "api_key": self.settings.openrouter_api_key,
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": self.settings.openrouter_transcription_model,
                },
                "hook_maker": {
                    "api_key": self.settings.openrouter_api_key,
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": self.settings.openrouter_tts_model,
                },
            },
            subtitle_language="en",
        )
        highlight = _candidate_to_core_highlight(candidate)
        section_path = output_dir / "_temp" / f"run_{candidate.run_id}_candidate_{candidate.candidate_number}.mp4"
        section_path.parent.mkdir(parents=True, exist_ok=True)
        video_path = core.download_video_section(
            source_url,
            highlight["start_time"],
            highlight["end_time"],
            str(section_path),
        )
        before = set(output_dir.glob("*/master.mp4"))
        core.process_clip(
            video_path,
            highlight,
            candidate.candidate_number,
            1,
            add_captions=captions_enabled,
            add_hook=hooks_enabled,
            pre_cut=True,
        )
        after = set(output_dir.glob("*/master.mp4"))
        created = sorted(after - before, key=lambda path: path.stat().st_mtime, reverse=True)
        if not created:
            created = sorted(output_dir.glob("*/master.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not created:
            raise RuntimeError("Clip processing finished without creating master.mp4")
        return created[0]


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
        self.settings = settings
        self.provider = GeminiTextProvider(settings)

    def find_highlights(
        self,
        youtube_url: str,
        count: int,
        subtitle_language: str = "en",
    ) -> list[HighlightDraft]:
        transcript, video_info = self._load_transcript(youtube_url, subtitle_language)
        prompt = (
            f"Find {count} short-form highlight candidates from the transcript below. "
            "Use only the transcript and metadata provided; do not invent topics that are not present. "
            "Return JSON only as an array of objects with title, start_time, end_time, virality_score, "
            "hook_text, and description. Times must come from the transcript and use HH:MM:SS,mmm.\n\n"
            f"Video title: {video_info.get('title', '')}\n"
            f"Channel: {video_info.get('channel', '')}\n"
            f"URL: {youtube_url}\n\n"
            f"Transcript:\n{transcript}"
        )
        raw_response = self.provider.generate_text(prompt)
        data = _parse_gemini_json(raw_response, "array")
        return [_highlight_draft_from_mapping(item) for item in data[:count]]

    def _load_transcript(self, youtube_url: str, subtitle_language: str) -> tuple[str, dict]:
        from clipper_core import AutoClipperCore

        work_dir = Path("data/manual_clipping")
        work_dir.mkdir(parents=True, exist_ok=True)
        core = AutoClipperCore(
            client=OpenAI(api_key=self.settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1"),
            output_dir=str(work_dir),
            ytdlp_path="yt_dlp_module",
            subtitle_language=subtitle_language,
            ai_providers={
                "highlight_finder": {
                    "api_key": self.settings.openrouter_api_key,
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": self.settings.openrouter_transcription_model,
                },
                "caption_maker": {
                    "api_key": self.settings.openrouter_api_key,
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": self.settings.openrouter_transcription_model,
                },
                "hook_maker": {
                    "api_key": self.settings.openrouter_api_key,
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": self.settings.openrouter_tts_model,
                },
            },
        )
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        core.temp_dir = work_dir / timestamp / "_temp"
        core.temp_dir.mkdir(parents=True, exist_ok=True)
        srt_path, video_info = core.download_subtitle_only(youtube_url)
        if not srt_path:
            core.subtitle_language = "none"
            video_path, _srt_path, fallback_video_info = core.download_video(youtube_url)
            transcript = core.transcribe_full_video(video_path)
            return transcript, fallback_video_info or video_info or {}
        transcript = core.parse_srt(srt_path)
        if not transcript.strip():
            raise ValueError("Subtitle transcript is empty")
        return transcript, video_info or {}


class ManualClippingService:
    def __init__(
        self,
        settings: Settings,
        highlight_finder: HighlightFinder | None = None,
        clip_processor: ClipProcessor | None = None,
        clipping_queue: ClippingQueue | None = None,
        metadata_generator: MetadataGenerator | None = None,
        youtube_publisher: Publisher | None = None,
        tiktok_publisher: Publisher | None = None,
    ):
        self.settings = settings
        self.highlight_finder = highlight_finder or GeminiHighlightFinder(settings)
        self.clip_processor = clip_processor or ExistingClipProcessor(settings)
        self.clipping_queue = clipping_queue or ClippingQueue()
        self.metadata_generator = metadata_generator or GeminiMetadataGenerator(settings)
        self.youtube_publisher = youtube_publisher
        self.tiktok_publisher = tiktok_publisher

    def start_run(self, session: Session, youtube_url: str) -> RunLog:
        defaults = ensure_workflow_defaults(session)
        run = RunLog(source_url=youtube_url, status="finding_highlights")
        session.add(run)
        session.commit()
        session.refresh(run)
        self.add_event(session, run, "started", "Manual Clipping started")

        try:
            drafts = self.highlight_finder.find_highlights(
                youtube_url,
                defaults.manual_highlight_candidates,
                defaults.subtitle_language,
            )
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
        if run is None:
            return []
        if run.cancellation_requested:
            run.status = "cancelled"
            self.add_event(session, run, "cancelled", "Run cancelled before clipping started")
            session.commit()
            return []
        if run.status != "selection_ready":
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
                if run.cancellation_requested:
                    run.status = "cancelled"
                    self.add_event(session, run, "cancelled", "Run cancelled before next clipping step")
                    session.commit()
                    return []
                output_path = self.clip_processor.process_highlight(
                    run.source_url,
                    candidate,
                    captions_enabled=defaults.captions_enabled,
                    hooks_enabled=defaults.hooks_enabled,
                )
                metadata = self.metadata_generator.generate_metadata(
                    run.source_url,
                    candidate,
                    settings.gemini_youtube_title_model,
                )
                clip = create_clip_record(
                    session,
                    settings,
                    output_path,
                    generated_title=metadata.title,
                    generated_description=metadata.description,
                    generated_hashtags=" ".join(metadata.hashtags),
                )
                summary = f"{metadata.title}: {clip.public_clip_link}"
                publish_summaries = self._publish_clip(session, defaults, clip, metadata)
                if publish_summaries:
                    summary = summary + "\n" + "\n".join(publish_summaries)
                links.append(summary)
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

    def _publish_clip(
        self,
        session: Session,
        defaults,
        clip: ClipRecord,
        metadata: PublishingMetadata,
    ) -> list[str]:
        summaries = []
        publishers = []
        if defaults.publish_youtube:
            publishers.append(("youtube", self.youtube_publisher))
        if defaults.publish_tiktok:
            publishers.append(("tiktok", self.tiktok_publisher))

        for platform, publisher in publishers:
            try:
                if publisher is None:
                    raise RuntimeError(f"{platform} preauthorized publisher is not configured")
                platform_url = publisher.publish(clip, metadata)
                attempt = PublishAttempt(
                    clip_record_id=clip.id,
                    platform=platform,
                    status="published",
                    platform_url=platform_url,
                )
                summaries.append(f"{platform}: published {platform_url}")
            except Exception as exc:
                attempt = PublishAttempt(
                    clip_record_id=clip.id,
                    platform=platform,
                    status="failed",
                    error_message=str(exc),
                )
                summaries.append(f"{platform}: failed {exc}")
            session.add(attempt)
        session.commit()
        return summaries

    def request_cancellation(self, session: Session, run_id: int) -> bool:
        run = session.get(RunLog, run_id)
        if run is None:
            return False
        run.cancellation_requested = True
        run.status = "cancellation_requested"
        self.add_event(session, run, "cancellation_requested", "Cancellation requested by Authorized Operator")
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
