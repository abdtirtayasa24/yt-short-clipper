import ipaddress
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

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


@dataclass
class DirectVideoSource:
    path: Path
    filename: str
    content_type: str | None
    file_size: int


@dataclass
class TelegramVideoUpload:
    file_id: str
    filename: str | None
    content_type: str | None
    file_size: int | None


SUPPORTED_DIRECT_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


class ManualClippingUserError(Exception):
    """Operator-facing Manual Clipping error with low-level details already recorded."""


class HighlightFinder(Protocol):
    def find_highlights(
        self,
        source_url: str,
        count: int,
        subtitle_language: str = "en",
        *,
        source_path: Path | None = None,
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


def _youtube_fallback_guidance() -> str:
    return "You can also retry with a direct video URL or by uploading the video to Telegram."


def _operator_error_message(error_message: str) -> str:
    text = error_message or ""
    lowered = text.lower()
    if "ffmpeg is not installed" in lowered or "ffmpeg" in lowered and "partial" in lowered:
        return (
            "FFmpeg is required for partial YouTube downloads on this server. "
            "Install/configure FFmpeg for the service runtime, then retry. "
            f"{_youtube_fallback_guidance()}"
        )
    if "sign in to confirm" in lowered or "not a bot" in lowered or "cookies" in lowered or "cookie" in lowered:
        return (
            "YouTube is requiring fresh YouTube cookies or bot verification for this VPS. "
            "Export fresh cookies from a logged-in browser session and update cookies.txt. "
            f"{_youtube_fallback_guidance()}"
        )
    if "requested format is not available" in lowered or "n challenge" in lowered or "challenge solving failed" in lowered:
        return (
            "YouTube challenge solving may be unavailable or incomplete, so yt-dlp cannot see downloadable video formats. "
            "Ensure Deno/challenge solver support is available to the service runtime. "
            f"{_youtube_fallback_guidance()}"
        )
    if "[youtube]" in lowered or "yt-dlp" in lowered or "youtube" in lowered:
        return (
            "YouTube access failed while yt-dlp was preparing this Manual Clipping run. "
            f"{_youtube_fallback_guidance()}"
        )
    return text


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
        source_path: Path | None = None,
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
        source_path: Path | None = None,
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
        if source_path is not None:
            video_path = self._cut_local_video_section(
                source_path,
                highlight["start_time"],
                highlight["end_time"],
                section_path,
            )
        else:
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

    def _cut_local_video_section(
        self,
        source_path: Path,
        start_time: str,
        end_time: str,
        output_path: Path,
    ) -> str:
        from utils.helpers import get_ffmpeg_path

        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            raise RuntimeError("FFmpeg is required to process direct video URLs")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(ffmpeg_path),
            "-y",
            "-ss",
            start_time.replace(",", "."),
            "-to",
            end_time.replace(",", "."),
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed to cut direct video section:\n{result.stderr}")
        if not output_path.exists():
            raise RuntimeError(f"Direct video section was not created at {output_path}")
        return str(output_path)


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


class DirectVideoIngestor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def is_direct_video_url(self, source_url: str) -> bool:
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        return Path(unquote(parsed.path)).suffix.lower() in SUPPORTED_DIRECT_VIDEO_EXTENSIONS

    def download(self, source_url: str, run_id: int) -> DirectVideoSource:
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Direct video URL must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("Direct video URL must not include credentials")
        self._validate_public_host(parsed.hostname)

        filename = self._safe_filename(Path(unquote(parsed.path)).name or f"source-{run_id}.mp4")
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_DIRECT_VIDEO_EXTENSIONS:
            raise ValueError("Direct video URL must point to a supported video file")

        request = Request(source_url, headers={"User-Agent": "yt-short-clipper/1.0"})
        try:
            with urlopen(request, timeout=30) as response:
                headers = response.headers
                content_type = self._header_value(headers, "Content-Type")
                normalized_content_type = content_type.split(";", 1)[0].strip().lower() if content_type else None
                content_length = self._header_value(headers, "Content-Length")
                content_length_bytes = self._parse_content_length(content_length)
                if content_length_bytes and content_length_bytes > self.settings.source_video_max_bytes:
                    raise ValueError(
                        f"Direct video is too large. Limit is {self.settings.source_video_max_bytes} bytes."
                    )
                if not self._is_supported_video_response(normalized_content_type, suffix):
                    raise ValueError("Direct video URL did not return a supported video content type")

                run_dir = self.settings.source_video_dir / str(run_id)
                run_dir.mkdir(parents=True, exist_ok=True)
                target_path = run_dir / filename
                bytes_written = 0
                with open(target_path, "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > self.settings.source_video_max_bytes:
                            output.close()
                            target_path.unlink(missing_ok=True)
                            raise ValueError(
                                f"Direct video is too large. Limit is {self.settings.source_video_max_bytes} bytes."
                            )
                        output.write(chunk)
        except (HTTPError, URLError) as exc:
            raise ValueError(f"Failed to download direct video URL: {exc}") from exc

        if bytes_written == 0:
            target_path.unlink(missing_ok=True)
            raise ValueError("Direct video URL downloaded an empty file")

        return DirectVideoSource(
            path=target_path,
            filename=filename,
            content_type=normalized_content_type,
            file_size=bytes_written,
        )

    def _validate_public_host(self, hostname: str | None) -> None:
        host = (hostname or "").lower()
        if host == "localhost":
            raise ValueError("Direct video URL must not point to localhost")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise ValueError("Direct video URL must point to a public host")

    def validate_video_metadata(self, filename: str, content_type: str | None, file_size: int | None) -> str:
        safe_filename = self._safe_filename(filename)
        suffix = Path(safe_filename).suffix.lower()
        normalized_content_type = content_type.split(";", 1)[0].strip().lower() if content_type else None
        if suffix not in SUPPORTED_DIRECT_VIDEO_EXTENSIONS and not (
            normalized_content_type and normalized_content_type.startswith("video/")
        ):
            raise ValueError("Upload must be a supported video file")
        if file_size is not None and file_size > self.settings.source_video_max_bytes:
            raise ValueError(f"Uploaded video is too large. Limit is {self.settings.source_video_max_bytes} bytes.")
        return safe_filename

    def _header_value(self, headers, name: str) -> str | None:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        return value

    def _parse_content_length(self, content_length: str | None) -> int | None:
        if not content_length:
            return None
        try:
            value = int(content_length)
        except ValueError as exc:
            raise ValueError("Direct video URL returned an invalid Content-Length header") from exc
        if value < 0:
            raise ValueError("Direct video URL returned an invalid Content-Length header")
        return value

    def _is_supported_video_response(self, content_type: str | None, suffix: str) -> bool:
        if content_type is None:
            return suffix in SUPPORTED_DIRECT_VIDEO_EXTENSIONS
        if content_type.startswith("video/"):
            return True
        return content_type == "application/octet-stream" and suffix in SUPPORTED_DIRECT_VIDEO_EXTENSIONS

    def _safe_filename(self, filename: str) -> str:
        name = Path(filename).name
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
        if not name:
            name = "source.mp4"
        return name


class GeminiHighlightFinder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider = GeminiTextProvider(settings)

    def find_highlights(
        self,
        source_url: str,
        count: int,
        subtitle_language: str = "en",
        *,
        source_path: Path | None = None,
    ) -> list[HighlightDraft]:
        if source_path is None:
            transcript, video_info = self._load_transcript(source_url, subtitle_language)
        else:
            transcript, video_info = self._load_local_transcript(source_path)
        prompt = (
            f"Find {count} short-form highlight candidates from the transcript below. "
            "Use only the transcript and metadata provided; do not invent topics that are not present. "
            "Return JSON only as an array of objects with title, start_time, end_time, virality_score, "
            "hook_text, and description. Times must come from the transcript and use HH:MM:SS,mmm.\n\n"
            f"Video title: {video_info.get('title', '')}\n"
            f"Channel: {video_info.get('channel', '')}\n"
            f"URL: {source_url}\n\n"
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

    def _load_local_transcript(self, source_path: Path) -> tuple[str, dict]:
        from clipper_core import AutoClipperCore

        if not self.settings.openrouter_api_key:
            raise ValueError("OpenRouter transcription is required for direct video URLs")
        work_dir = Path("data/manual_clipping")
        work_dir.mkdir(parents=True, exist_ok=True)
        core = AutoClipperCore(
            client=OpenAI(api_key=self.settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1"),
            output_dir=str(work_dir),
            ytdlp_path="yt_dlp_module",
            subtitle_language="none",
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
        transcript = core.transcribe_full_video(str(source_path))
        if not transcript.strip():
            raise ValueError("Direct video transcription is empty")
        return transcript, {"title": source_path.name, "channel": ""}


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
        direct_video_ingestor: DirectVideoIngestor | None = None,
    ):
        self.settings = settings
        self.highlight_finder = highlight_finder or GeminiHighlightFinder(settings)
        self.clip_processor = clip_processor or ExistingClipProcessor(settings)
        self.clipping_queue = clipping_queue or ClippingQueue()
        self.metadata_generator = metadata_generator or GeminiMetadataGenerator(settings)
        self.youtube_publisher = youtube_publisher
        self.tiktok_publisher = tiktok_publisher
        self.direct_video_ingestor = direct_video_ingestor or DirectVideoIngestor(settings)

    def prepare_telegram_file_run(self, session: Session, upload: TelegramVideoUpload) -> tuple[RunLog, Path]:
        filename = self.direct_video_ingestor.validate_video_metadata(
            upload.filename or f"telegram-{upload.file_id}.mp4",
            upload.content_type,
            upload.file_size,
        )
        run = RunLog(
            source_url=f"telegram://{upload.file_id}",
            source_type="telegram_file",
            source_filename=filename,
            source_content_type=upload.content_type,
            source_file_size=upload.file_size,
            status="finding_highlights",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        target_path = self.settings.source_video_dir / str(run.id) / filename
        target_path.parent.mkdir(parents=True, exist_ok=True)
        run.source_path = str(target_path)
        self.add_event(session, run, "started", "Manual Clipping started from Telegram upload")
        session.commit()
        session.refresh(run)
        return run, target_path

    def complete_telegram_file_run(self, session: Session, run_id: int) -> RunLog:
        run = session.get(RunLog, run_id)
        if run is None or run.source_type != "telegram_file" or not run.source_path:
            raise ValueError("Telegram upload Run Log is not ready")
        source_path = Path(run.source_path)
        if not source_path.exists() or source_path.stat().st_size == 0:
            raise ValueError("Telegram video download did not create a usable file")
        if run.source_file_size is None:
            run.source_file_size = source_path.stat().st_size
        self.add_event(session, run, "source_downloaded", f"Downloaded Telegram upload to {run.source_filename}")
        return self._find_and_store_highlights(session, run, source_path=source_path)

    def fail_run(self, session: Session, run_id: int, error_message: str) -> None:
        run = session.get(RunLog, run_id)
        if run is None:
            return
        run.status = "failed"
        run.error_message = error_message
        self.add_event(session, run, "error", error_message)
        session.commit()

    def start_run(self, session: Session, source_url: str) -> RunLog:
        source_type = "direct_video_url" if self.direct_video_ingestor.is_direct_video_url(source_url) else "youtube"
        run = RunLog(source_url=source_url, source_type=source_type, status="finding_highlights")
        session.add(run)
        session.commit()
        session.refresh(run)
        self.add_event(session, run, "started", "Manual Clipping started")

        try:
            source_path = None
            if source_type == "direct_video_url":
                direct_source = self.direct_video_ingestor.download(source_url, run.id)
                run.source_path = str(direct_source.path)
                run.source_filename = direct_source.filename
                run.source_content_type = direct_source.content_type
                run.source_file_size = direct_source.file_size
                source_path = direct_source.path
                self.add_event(session, run, "source_downloaded", f"Downloaded direct video URL to {direct_source.filename}")
            return self._find_and_store_highlights(session, run, source_path=source_path)
        except Exception as exc:
            raw_error = str(exc)
            run.status = "failed"
            run.error_message = raw_error
            self.add_event(session, run, "error", raw_error)
            session.commit()
            operator_message = _operator_error_message(raw_error)
            if operator_message != raw_error:
                raise ManualClippingUserError(operator_message) from exc
            raise

    def _find_and_store_highlights(self, session: Session, run: RunLog, *, source_path: Path | None = None) -> RunLog:
        defaults = ensure_workflow_defaults(session)
        if source_path is None:
            drafts = self.highlight_finder.find_highlights(
                run.source_url,
                defaults.manual_highlight_candidates,
                defaults.subtitle_language,
            )
        else:
            drafts = self.highlight_finder.find_highlights(
                run.source_url,
                defaults.manual_highlight_candidates,
                defaults.subtitle_language,
                source_path=source_path,
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

    def select_candidates(self, session: Session, run_id: int, numbers: list[int]) -> bool:
        run = session.get(RunLog, run_id)
        if run is None or run.status not in {"awaiting_selection", "selection_ready"}:
            return False
        selected = set(numbers)
        for candidate in run.highlight_candidates:
            candidate.selected = candidate.candidate_number in selected
        run.status = "selection_ready"
        run.selected_highlights = ",".join(str(number) for number in numbers)
        self.add_event(session, run, "selected", f"Selected highlights: {run.selected_highlights}")
        session.commit()
        return True

    def toggle_candidate_selection(self, session: Session, run_id: int, candidate_number: int) -> bool | None:
        run = session.get(RunLog, run_id)
        if run is None or run.status not in {"awaiting_selection", "selection_ready"}:
            return None
        candidate = next(
            (item for item in run.highlight_candidates if item.candidate_number == candidate_number),
            None,
        )
        if candidate is None:
            return None
        candidate.selected = not candidate.selected
        selected_numbers = [
            item.candidate_number for item in sorted(run.highlight_candidates, key=lambda item: item.candidate_number) if item.selected
        ]
        run.selected_highlights = ",".join(str(number) for number in selected_numbers) or None
        run.status = "selection_ready" if selected_numbers else "awaiting_selection"
        action = "Selected" if candidate.selected else "Unselected"
        self.add_event(session, run, "selected", f"{action} highlight {candidate.candidate_number}")
        session.commit()
        return candidate.selected

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
                source_path = Path(run.source_path) if run.source_path else None
                if source_path is None:
                    output_path = self.clip_processor.process_highlight(
                        run.source_url,
                        candidate,
                        captions_enabled=defaults.captions_enabled,
                        hooks_enabled=defaults.hooks_enabled,
                    )
                else:
                    output_path = self.clip_processor.process_highlight(
                        run.source_url,
                        candidate,
                        captions_enabled=defaults.captions_enabled,
                        hooks_enabled=defaults.hooks_enabled,
                        source_path=source_path,
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
            raw_error = str(exc)
            run.status = "failed"
            run.error_message = raw_error
            self.add_event(session, run, "error", raw_error)
            session.commit()
            operator_message = _operator_error_message(raw_error)
            if operator_message != raw_error:
                raise ManualClippingUserError(operator_message) from exc
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
        if run is None or run.status not in {"finding_highlights", "awaiting_selection", "selection_ready"}:
            return False
        run.status = "cancelled"
        self.add_event(session, run, "cancelled", "Manual Clipping cancelled")
        session.commit()
        return True

    def add_event(self, session: Session, run: RunLog, event_type: str, message: str) -> None:
        session.add(RunEvent(run_id=run.id, event_type=event_type, message=message, created_at=datetime.now(timezone.utc)))
