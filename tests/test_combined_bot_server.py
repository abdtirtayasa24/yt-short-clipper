import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from bot_app.ai_providers import GeminiTextProvider, OpenRouterAudioAdapter
from bot_app.clip_archive import cleanup_expired_clips, create_clip_record
from bot_app.manual_clipping import GeminiHighlightFinder, HighlightDraft, ManualClippingService, PublishingMetadata
from bot_app.database import create_session_factory, initialize_database
from bot_app.main import create_app
from bot_app.models import ClipRecord, HighlightCandidate, PublishAttempt, RunEvent, RunLog, WorkflowDefaults
from bot_app.scheduler import add_daily_schedule, fire_schedule
from bot_app.source_queue import add_source_videos, consume_source_video, get_pending_source_videos
from bot_app.settings import Settings
from bot_app.telegram_bot import AuthorizedOperatorTelegramBot
from clipper_core import AutoClipperCore


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="https://clips.example.com",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
        telegram_bot_token="123:test-token",
        telegram_authorized_chat_id=123456,
        gemini_api_key="gemini-test-key",
        openrouter_api_key="openrouter-test-key",
        openrouter_tts_model="canopylabs/orpheus-3b-0.1-ft",
        openrouter_tts_voice="josh",
        clip_archive_dir=tmp_path / "clips",
        source_video_dir=tmp_path / "source_videos",
    )


class FakeMessage:
    def __init__(self, video=None, document=None):
        self.replies = []
        self.reply_markups = []
        self.video = video
        self.document = document

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)
        self.reply_markups.append(reply_markup)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []
        self.edit_markups = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)
        self.edit_markups.append(reply_markup)


class FakeUpdate:
    def __init__(self, chat_id, message=None, callback_query=None):
        self.effective_chat = FakeChat(chat_id)
        self.message = message if message is not None else (None if callback_query is not None else FakeMessage())
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot


class FakeTelegramBot:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeUpdater:
    def __init__(self, calls):
        self.calls = calls

    async def start_polling(self, **kwargs):
        self.calls.append(("start_polling", kwargs))

    async def stop(self):
        self.calls.append(("updater_stop", {}))


class FakeTelegramApplication:
    def __init__(self):
        self.calls = []
        self.handlers = []
        self.updater = FakeUpdater(self.calls)

    def add_handler(self, handler):
        self.handlers.append(handler)

    async def initialize(self):
        self.calls.append(("initialize", {}))

    async def start(self):
        self.calls.append(("application_start", {}))

    async def stop(self):
        self.calls.append(("application_stop", {}))

    async def shutdown(self):
        self.calls.append(("shutdown", {}))


class FakeApplicationBuilder:
    def __init__(self, application):
        self.application = application

    def token(self, token):
        self.token_value = token
        return self

    def build(self):
        return self.application


def test_telegram_bot_start_polls_for_updates_and_stop_shuts_down(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    fake_application = FakeTelegramApplication()
    monkeypatch.setattr(
        "bot_app.telegram_bot.ApplicationBuilder",
        lambda: FakeApplicationBuilder(fake_application),
    )

    async def run_test():
        bot = AuthorizedOperatorTelegramBot(settings)
        await bot.start()

        assert bot.started is True
        assert len(fake_application.handlers) == 15
        assert fake_application.calls == [
            ("initialize", {}),
            ("start_polling", {"drop_pending_updates": True}),
            ("application_start", {}),
        ]

        await bot.stop()

        assert bot.started is False
        assert fake_application.calls[-3:] == [
            ("updater_stop", {}),
            ("application_stop", {}),
            ("shutdown", {}),
        ]

    asyncio.run(run_test())


def test_fastapi_lifespan_starts_and_stops_telegram_bot(tmp_path):
    settings = make_settings(tmp_path)
    telegram_bot = FakeTelegramBot()

    app = create_app(settings, telegram_bot=telegram_bot)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert telegram_bot.started is True
        assert telegram_bot.stopped is False

    assert telegram_bot.stopped is True


def test_status_shows_operational_state_and_source_queue_summary(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FakeHighlightFinder())
        service.clipping_queue.active_run_id = 42
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            from bot_app.source_queue import add_source_videos

            add_source_videos(session, ["https://youtu.be/pending"])
            session.add(RunLog(source_url="https://youtu.be/recent", status="queued"))
            session.commit()

        status_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_status(status_update, None) is True
        reply = status_update.message.replies[0]
        assert "active run: 42" in reply
        assert "queued runs: 1" in reply
        assert "recent runs:" in reply
        assert "Source Video Queue: pending=1" in reply

    asyncio.run(run_test())


def test_cancel_requests_active_run_cancellation_and_worker_stops_before_next_clip(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        processor = FakeClipProcessor(settings.clip_archive_dir)
        service = ManualClippingService(
            settings,
            FakeHighlightFinder(),
            clip_processor=processor,
            metadata_generator=FakeMetadataGenerator(),
        )
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = service.start_run(session, "https://youtu.be/manual")
            service.select_candidates(session, run.id, [1, 2])
            service.clipping_queue.active_run_id = run.id

        cancel_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_cancel(cancel_update, FakeContext()) is True
        assert "Cancellation requested for Run Log 1" in cancel_update.message.replies[0]

        service.clipping_queue.active_run_id = None
        with session_factory() as session:
            links = service.process_selected_run(session, settings, 1)
            run = session.get(RunLog, 1)
            assert links == []
            assert run.status == "cancelled"
            assert run.cancellation_requested is True
            assert processor.calls == []
            assert session.scalars(select(RunEvent).where(RunEvent.event_type == "cancelled")).all()

    asyncio.run(run_test())


def test_start_and_help_show_inline_home_menu(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        bot = AuthorizedOperatorTelegramBot(settings)

        start_update = FakeUpdate(settings.telegram_authorized_chat_id)
        help_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_start(start_update, None) is True
        assert await bot.handle_help(help_update, None) is True

        start_markup = start_update.message.reply_markups[0]
        help_markup = help_update.message.reply_markups[0]
        start_buttons = [button.text for row in start_markup.inline_keyboard for button in row]
        assert start_buttons == ["New Clip", "Source Queue", "Schedule", "Workflow Defaults", "Status", "Auth"]
        assert help_markup is not None
        assert "/status" in help_update.message.replies[0]

    asyncio.run(run_test())


def test_home_menu_callbacks_route_to_status_auth_and_prompts(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        status_query = FakeCallbackQuery("menu:status")
        assert await bot.handle_menu_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=status_query), FakeContext()) is True
        assert "status: ok" in status_query.edits[0]

        auth_query = FakeCallbackQuery("menu:auth")
        assert await bot.handle_menu_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=auth_query), FakeContext()) is True
        assert "Preauthorization Setup" in auth_query.edits[0]

        clip_query = FakeCallbackQuery("menu:new_clip")
        assert await bot.handle_menu_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=clip_query), FakeContext()) is True
        assert "/clip" in clip_query.edits[0]

        unauthorized = FakeCallbackQuery("menu:status")
        assert await bot.handle_menu_callback(FakeUpdate(999, callback_query=unauthorized), FakeContext()) is False
        assert unauthorized.answers == ["Unauthorized chat."]

    asyncio.run(run_test())


def test_authorized_operator_commands_work_and_unknown_chats_are_rejected(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        bot = AuthorizedOperatorTelegramBot(settings)

        unauthorized_update = FakeUpdate(999)
        assert await bot.handle_start(unauthorized_update, None) is False
        assert unauthorized_update.message.replies == ["Unauthorized chat."]

        start_update = FakeUpdate(settings.telegram_authorized_chat_id)
        help_update = FakeUpdate(settings.telegram_authorized_chat_id)
        status_update = FakeUpdate(settings.telegram_authorized_chat_id)

        assert await bot.handle_start(start_update, None) is True
        assert await bot.handle_help(help_update, None) is True
        assert await bot.handle_status(status_update, None) is True

        assert "Bot Control Mode" in start_update.message.replies[0]
        assert "/status" in help_update.message.replies[0]
        assert "status: ok" in status_update.message.replies[0]

    asyncio.run(run_test())


class FailingHighlightFinder:
    def find_highlights(self, youtube_url, count, subtitle_language="en"):
        raise ValueError("Gemini did not return valid highlight JSON")


class FailingHighlightFinderWithMessage:
    def __init__(self, message):
        self.message = message

    def find_highlights(self, youtube_url, count, subtitle_language="en"):
        raise ValueError(self.message)


class FakeHighlightFinder:
    def __init__(self):
        self.calls = []

    def find_highlights(self, youtube_url, count, subtitle_language="en"):
        self.calls.append((youtube_url, count, subtitle_language))
        return [
            HighlightDraft(
                title="Great moment",
                start_time="00:00:01,000",
                end_time="00:01:01,000",
                virality_score=9,
                hook_text="Watch this",
                description="A strong highlight",
            )
            for _ in range(count)
        ]


class FakeDirectVideoHighlightFinder:
    def __init__(self):
        self.calls = []

    def find_highlights(self, source_url, count, subtitle_language="en", *, source_path=None):
        self.calls.append((source_url, count, subtitle_language, source_path))
        assert source_path is not None
        assert Path(source_path).exists()
        return [
            HighlightDraft(
                title="Direct video moment",
                start_time="00:00:01,000",
                end_time="00:00:11,000",
                virality_score=8,
                hook_text="Direct hook",
                description="A direct video highlight",
            )
        ]


class FakeHttpResponse:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self.body = body
        self.headers = headers
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        if self.offset >= len(self.body):
            return b""
        if size is None or size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeTelegramUpload:
    def __init__(self, file_id="file-1", file_name="upload.mp4", mime_type="video/mp4", file_size=14):
        self.file_id = file_id
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = file_size


class FakeTelegramFile:
    def __init__(self, body=b"fake mp4 bytes"):
        self.body = body
        self.downloads = []

    async def download_to_drive(self, custom_path):
        path = Path(custom_path)
        path.write_bytes(self.body)
        self.downloads.append(path)
        return path


class FakeTelegramDownloadBot:
    def __init__(self, telegram_file=None):
        self.telegram_file = telegram_file or FakeTelegramFile()
        self.file_ids = []

    async def get_file(self, file_id):
        self.file_ids.append(file_id)
        return self.telegram_file


def test_gemini_highlight_finder_accepts_fenced_json_response():
    class FakeProvider:
        def generate_text(self, prompt):
            return """```json
[
  {
    "title": "Great moment",
    "start_time": "00:00:01,000",
    "end_time": "00:01:01,000",
    "virality_score": 9,
    "hook_text": "Watch this",
    "description": "A strong highlight"
  }
]
```"""

    finder = GeminiHighlightFinder.__new__(GeminiHighlightFinder)
    finder.provider = FakeProvider()
    finder._load_transcript = lambda youtube_url, subtitle_language: (
        "00:00:01,000 --> 00:01:01,000 A real transcript line from the video",
        {"title": "Real video", "channel": "Real channel"},
    )

    highlights = finder.find_highlights("https://youtu.be/manual", 1)

    assert highlights == [
        HighlightDraft(
            title="Great moment",
            start_time="00:00:01,000",
            end_time="00:01:01,000",
            virality_score=9,
            hook_text="Watch this",
            description="A strong highlight",
        )
    ]


def test_gemini_highlight_finder_uses_local_transcript_for_direct_video(tmp_path):
    class FakeProvider:
        def __init__(self):
            self.prompts = []

        def generate_text(self, prompt):
            self.prompts.append(prompt)
            return """[
  {
    "title": "Local moment",
    "start_time": "00:00:02,000",
    "end_time": "00:00:12,000",
    "virality_score": 7,
    "hook_text": "Local hook",
    "description": "A local video highlight"
  }
]"""

    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"video")
    finder = GeminiHighlightFinder.__new__(GeminiHighlightFinder)
    finder.provider = FakeProvider()
    finder._load_local_transcript = lambda path: (
        "00:00:02,000 --> 00:00:12,000 A local transcript line",
        {"title": "source.mp4", "channel": ""},
    )

    highlights = finder.find_highlights("https://media.example.com/source.mp4", 1, source_path=source_path)

    assert highlights == [
        HighlightDraft(
            title="Local moment",
            start_time="00:00:02,000",
            end_time="00:00:12,000",
            virality_score=7,
            hook_text="Local hook",
            description="A local video highlight",
        )
    ]
    assert "A local transcript line" in finder.provider.prompts[0]
    assert "https://media.example.com/source.mp4" in finder.provider.prompts[0]


def test_youtube_failures_return_actionable_operator_guidance_and_keep_raw_events(tmp_path):
    async def assert_guidance(raw_error, expected_text):
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FailingHighlightFinderWithMessage(raw_error))
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(update, FakeContext(["https://youtu.be/manual"])) is False
        reply = update.message.replies[0]
        assert expected_text in reply
        assert "direct video URL" in reply
        assert "uploading the video to Telegram" in reply

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.status == "failed"
            assert run.error_message == raw_error
            event = session.scalars(select(RunEvent).where(RunEvent.event_type == "error")).one()
            assert event.message == raw_error

    awaitable_errors = [
        ("ERROR: You have requested downloading the video partially, but ffmpeg is not installed. Aborting", "FFmpeg is required"),
        ("ERROR: [youtube] abc: Requested format is not available. Use --list-formats", "YouTube challenge solving"),
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies", "fresh YouTube cookies"),
    ]

    for raw_error, expected_text in awaitable_errors:
        asyncio.run(assert_guidance(raw_error, expected_text))
        tmp_path.joinpath("bot.db").unlink(missing_ok=True)


def test_clip_start_failure_replies_and_records_failed_run(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FailingHighlightFinder())
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(update, FakeContext(["https://youtu.be/manual"])) is False
        assert "Manual Clipping failed" in update.message.replies[0]
        assert "Gemini did not return valid highlight JSON" in update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.status == "failed"
            assert "Gemini did not return valid highlight JSON" in run.error_message

    asyncio.run(run_test())


def test_authorized_operator_can_start_telegram_video_manual_clipping(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        finder = FakeDirectVideoHighlightFinder()
        service = ManualClippingService(settings, finder)
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)
        download_bot = FakeTelegramDownloadBot()

        message = FakeMessage(video=FakeTelegramUpload(file_id="video-file", file_name="telegram-video.mp4"))
        update = FakeUpdate(settings.telegram_authorized_chat_id, message=message)
        assert await bot.handle_video_upload(update, FakeContext(bot=download_bot)) is True
        assert "Run Log 1 highlight candidates" in message.replies[0]
        assert "Direct video moment" in message.replies[0]
        assert download_bot.file_ids == ["video-file"]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.source_type == "telegram_file"
            assert run.source_filename == "telegram-video.mp4"
            assert run.source_content_type == "video/mp4"
            assert run.source_file_size == 14
            assert run.source_path is not None
            assert Path(run.source_path).exists()
            assert Path(run.source_path).read_bytes() == b"fake mp4 bytes"

        assert len(finder.calls) == 1
        assert finder.calls[0][3] == Path(run.source_path)

    asyncio.run(run_test())


def test_authorized_operator_can_start_telegram_video_document_manual_clipping(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FakeDirectVideoHighlightFinder())
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)
        document = FakeTelegramUpload(file_id="document-file", file_name="document-video.webm", mime_type="application/octet-stream")
        message = FakeMessage(document=document)

        assert await bot.handle_video_upload(
            FakeUpdate(settings.telegram_authorized_chat_id, message=message),
            FakeContext(bot=FakeTelegramDownloadBot()),
        ) is True
        assert "Run Log 1 highlight candidates" in message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.source_type == "telegram_file"
            assert run.source_filename == "document-video.webm"
            assert Path(run.source_path).exists()

    asyncio.run(run_test())


def test_telegram_video_upload_rejects_unsupported_and_oversized_files(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        settings.source_video_max_bytes = 10
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FakeDirectVideoHighlightFinder())
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        unsupported_message = FakeMessage(document=FakeTelegramUpload(file_name="notes.txt", mime_type="text/plain", file_size=5))
        assert await bot.handle_video_upload(
            FakeUpdate(settings.telegram_authorized_chat_id, message=unsupported_message),
            FakeContext(bot=FakeTelegramDownloadBot()),
        ) is False
        assert "supported video file" in unsupported_message.replies[0]

        oversized_message = FakeMessage(video=FakeTelegramUpload(file_name="large.mp4", file_size=11))
        assert await bot.handle_video_upload(
            FakeUpdate(settings.telegram_authorized_chat_id, message=oversized_message),
            FakeContext(bot=FakeTelegramDownloadBot()),
        ) is False
        assert "too large" in oversized_message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            assert session.scalars(select(RunLog)).all() == []

    asyncio.run(run_test())


def test_telegram_video_upload_rejects_unknown_chats_without_creating_run_logs(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)
        message = FakeMessage(video=FakeTelegramUpload())

        assert await bot.handle_video_upload(FakeUpdate(999, message=message), FakeContext(bot=FakeTelegramDownloadBot())) is False
        assert message.replies == ["Unauthorized chat."]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            assert session.scalars(select(RunLog)).all() == []

    asyncio.run(run_test())


def test_authorized_operator_can_start_direct_video_url_manual_clipping(monkeypatch, tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        finder = FakeDirectVideoHighlightFinder()
        service = ManualClippingService(settings, finder)
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        def fake_urlopen(request, timeout=30):
            return FakeHttpResponse(
                b"fake mp4 bytes",
                {"Content-Type": "video/mp4", "Content-Length": "14"},
            )

        monkeypatch.setattr("bot_app.manual_clipping.urlopen", fake_urlopen)

        update = FakeUpdate(settings.telegram_authorized_chat_id)
        direct_url = "https://media.example.com/videos/source.mp4"
        assert await bot.handle_clip(update, FakeContext([direct_url])) is True
        assert "Run Log 1 highlight candidates" in update.message.replies[0]
        assert "Direct video moment" in update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.source_url == direct_url
            assert run.source_type == "direct_video_url"
            assert run.source_content_type == "video/mp4"
            assert run.source_file_size == 14
            assert run.source_filename == "source.mp4"
            assert run.source_path is not None
            assert Path(run.source_path).exists()
            assert Path(run.source_path).read_bytes() == b"fake mp4 bytes"

        assert len(finder.calls) == 1
        assert finder.calls[0][0:3] == (direct_url, 5, "en")

    asyncio.run(run_test())


def test_direct_video_url_rejects_non_video_response(monkeypatch, tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FakeDirectVideoHighlightFinder())
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        def fake_urlopen(request, timeout=30):
            return FakeHttpResponse(
                b"<html>not a video</html>",
                {"Content-Type": "text/html", "Content-Length": "24"},
            )

        monkeypatch.setattr("bot_app.manual_clipping.urlopen", fake_urlopen)

        update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(update, FakeContext(["https://media.example.com/not-video.mp4"])) is False
        assert "Manual Clipping failed" in update.message.replies[0]
        assert "supported video content type" in update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.status == "failed"
            assert run.source_type == "direct_video_url"
            assert run.source_path is None

    asyncio.run(run_test())


def test_direct_video_url_rejects_oversized_response(monkeypatch, tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        settings.source_video_max_bytes = 10
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FakeDirectVideoHighlightFinder())
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        def fake_urlopen(request, timeout=30):
            return FakeHttpResponse(
                b"too many bytes",
                {"Content-Type": "video/mp4", "Content-Length": "14"},
            )

        monkeypatch.setattr("bot_app.manual_clipping.urlopen", fake_urlopen)

        update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(update, FakeContext(["https://media.example.com/large.mp4"])) is False
        assert "Manual Clipping failed" in update.message.replies[0]
        assert "too large" in update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.status == "failed"
            assert run.source_path is None

    asyncio.run(run_test())


def test_direct_video_url_rejects_private_ip_hosts(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FakeDirectVideoHighlightFinder())
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(update, FakeContext(["http://169.254.169.254/video.mp4"])) is False
        assert "Manual Clipping failed" in update.message.replies[0]
        assert "public host" in update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.scalars(select(RunLog)).one()
            assert run.status == "failed"
            assert run.source_type == "direct_video_url"

    asyncio.run(run_test())


def test_authorized_operator_can_start_select_and_cancel_manual_clipping(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        finder = FakeHighlightFinder()
        service = ManualClippingService(settings, finder)
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        start_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(start_update, FakeContext(["https://youtu.be/manual"])) is True
        reply = start_update.message.replies[0]
        assert "Run Log 1 highlight candidates" in reply
        assert "Great moment" in reply
        assert "00:00:01,000 - 00:01:01,000" in reply
        assert "Virality: 9" in reply
        assert "Hook: Watch this" in reply
        assert "Description: A strong highlight" in reply
        assert finder.calls == [("https://youtu.be/manual", 5, "en")]

        select_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(select_update, FakeContext(["select", "1", "1", "3"])) is True
        assert "Selected highlights for Run Log 1" in select_update.message.replies[0]

        second_start = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(second_start, FakeContext(["https://youtu.be/cancel"])) is True
        cancel_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(cancel_update, FakeContext(["cancel", "2"])) is True
        assert "Cancelled Run Log 2" in cancel_update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            runs = session.scalars(select(RunLog).order_by(RunLog.id)).all()
            assert runs[0].status == "selection_ready"
            assert runs[0].selected_highlights == "1,3"
            assert runs[1].status == "cancelled"
            assert len(session.scalars(select(HighlightCandidate)).all()) == 10
            assert len(session.scalars(select(RunEvent)).all()) >= 5

    asyncio.run(run_test())


class FakePublisher:
    def __init__(self, platform):
        self.platform = platform
        self.calls = []

    def publish(self, clip, metadata):
        self.calls.append((clip.archive_path, metadata.title))
        return f"https://{self.platform}.example.com/{clip.clip_id}"


class FakeMetadataGenerator:
    def __init__(self):
        self.calls = []

    def generate_metadata(self, source_url, candidate, model):
        self.calls.append((source_url, candidate.candidate_number, model))
        return PublishingMetadata(
            title=f"Title {candidate.candidate_number}",
            description=f"Description {candidate.candidate_number}",
            hashtags=["#shorts", "#viral"],
        )


class FakeClipProcessor:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.calls = []

    def process_highlight(self, source_url, candidate, *, captions_enabled, hooks_enabled):
        self.calls.append((source_url, candidate.candidate_number, captions_enabled, hooks_enabled))
        output_path = self.output_dir / f"clip-{candidate.candidate_number}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"clip-{candidate.candidate_number}".encode())
        return output_path


class FakeDirectClipProcessor:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.calls = []

    def process_highlight(self, source_url, candidate, *, captions_enabled, hooks_enabled, source_path=None):
        assert source_path is not None
        assert Path(source_path).exists()
        self.calls.append((source_url, candidate.candidate_number, captions_enabled, hooks_enabled, source_path))
        output_path = self.output_dir / f"direct-clip-{candidate.candidate_number}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"direct-clip-{candidate.candidate_number}".encode())
        return output_path


def test_highlight_review_includes_inline_selection_buttons(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        service = ManualClippingService(settings, FakeHighlightFinder())
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(update, FakeContext(["https://youtu.be/manual"])) is True
        markup = update.message.reply_markups[0]
        assert markup is not None
        callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert "clip:toggle:1:1" in callback_data
        assert "clip:process:1" in callback_data
        assert "clip:cancel:1" in callback_data

    asyncio.run(run_test())


def test_inline_highlight_buttons_toggle_process_cancel_and_reject_unauthorized(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        processor = FakeClipProcessor(settings.clip_archive_dir)
        service = ManualClippingService(
            settings,
            FakeHighlightFinder(),
            clip_processor=processor,
            metadata_generator=FakeMetadataGenerator(),
        )
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        assert await bot.handle_clip(FakeUpdate(settings.telegram_authorized_chat_id), FakeContext(["https://youtu.be/manual"])) is True

        toggle = FakeCallbackQuery("clip:toggle:1:1")
        assert await bot.handle_clip_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=toggle), FakeContext()) is True
        assert toggle.answers == ["Selected highlight 1."]
        assert "✅ 1" in [button.text for row in toggle.edit_markups[0].inline_keyboard for button in row]

        with create_session_factory(settings.database_url)() as session:
            candidate = session.scalars(select(HighlightCandidate).where(HighlightCandidate.candidate_number == 1)).first()
            assert candidate.selected is True
            assert session.get(RunLog, 1).status == "selection_ready"

        untoggle = FakeCallbackQuery("clip:toggle:1:1")
        assert await bot.handle_clip_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=untoggle), FakeContext()) is True
        assert untoggle.answers == ["Unselected highlight 1."]
        with create_session_factory(settings.database_url)() as session:
            candidate = session.scalars(select(HighlightCandidate).where(HighlightCandidate.candidate_number == 1)).first()
            assert candidate.selected is False
            assert session.get(RunLog, 1).status == "awaiting_selection"

        assert await bot.handle_clip_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=FakeCallbackQuery("clip:toggle:1:2")), FakeContext()) is True
        process = FakeCallbackQuery("clip:process:1")
        assert await bot.handle_clip_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=process), FakeContext()) is True
        assert "Public Clip Links" in process.edits[0]
        assert processor.calls == [("https://youtu.be/manual", 2, True, True)]

        assert await bot.handle_clip(FakeUpdate(settings.telegram_authorized_chat_id), FakeContext(["https://youtu.be/cancel"])) is True
        cancel = FakeCallbackQuery("clip:cancel:2")
        assert await bot.handle_clip_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=cancel), FakeContext()) is True
        assert "Cancelled Run Log 2" in cancel.edits[0]

        unauthorized = FakeCallbackQuery("clip:toggle:1:1")
        assert await bot.handle_clip_callback(FakeUpdate(999, callback_query=unauthorized), FakeContext()) is False
        assert unauthorized.answers == ["Unauthorized chat."]

    asyncio.run(run_test())


def test_defaults_command_shows_inline_toggles_and_callbacks_update_defaults(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        defaults_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_defaults(defaults_update, FakeContext()) is True
        assert "Workflow Defaults" in defaults_update.message.replies[0]
        assert "YouTube preauth: missing" in defaults_update.message.replies[0]
        callback_data = [button.callback_data for row in defaults_update.message.reply_markups[0].inline_keyboard for button in row]
        assert "defaults:toggle:captions" in callback_data
        assert "defaults:toggle:publish_youtube" in callback_data

        captions = FakeCallbackQuery("defaults:toggle:captions")
        assert await bot.handle_defaults_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=captions), FakeContext()) is True
        assert "captions: off" in captions.edits[0]

        youtube = FakeCallbackQuery("defaults:toggle:publish_youtube")
        assert await bot.handle_defaults_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=youtube), FakeContext()) is True
        assert "publish_youtube: on" in youtube.edits[0]
        assert "YouTube preauth: missing" in youtube.edits[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            defaults = session.get(WorkflowDefaults, 1)
            assert defaults.captions_enabled is False
            assert defaults.publish_youtube is True

    asyncio.run(run_test())


def test_defaults_callback_rejects_unauthorized_chat(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)
        query = FakeCallbackQuery("defaults:toggle:captions")

        assert await bot.handle_defaults_callback(FakeUpdate(999, callback_query=query), FakeContext()) is False
        assert query.answers == ["Unauthorized chat."]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            defaults = session.get(WorkflowDefaults, 1)
            assert defaults.captions_enabled is True

    asyncio.run(run_test())


def test_auth_command_reports_preauthorization_status(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        settings.youtube_credentials_path = tmp_path / "youtube.json"
        settings.tiktok_session_path = tmp_path / "tiktok.session"
        settings.youtube_credentials_path.write_text("{}")
        bot = AuthorizedOperatorTelegramBot(settings)

        auth_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_auth(auth_update, FakeContext()) is True
        reply = auth_update.message.replies[0]
        assert "YouTube: preauthorized" in reply
        assert "TikTok: missing" in reply
        assert "Preauthorization Setup" in reply
        assert "VPS" in reply

    asyncio.run(run_test())


def test_authorized_operator_processes_selected_highlights_into_public_clip_links(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        finder = FakeHighlightFinder()
        processor = FakeClipProcessor(settings.clip_archive_dir)
        metadata_generator = FakeMetadataGenerator()
        youtube_publisher = FakePublisher("youtube")
        tiktok_publisher = FakePublisher("tiktok")
        service = ManualClippingService(
            settings,
            finder,
            clip_processor=processor,
            metadata_generator=metadata_generator,
            youtube_publisher=youtube_publisher,
            tiktok_publisher=tiktok_publisher,
        )
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        assert await bot.handle_clip(
            FakeUpdate(settings.telegram_authorized_chat_id),
            FakeContext(["https://youtu.be/manual"]),
        ) is True
        assert await bot.handle_clip(
            FakeUpdate(settings.telegram_authorized_chat_id),
            FakeContext(["select", "1", "1", "3"]),
        ) is True
        assert await bot.handle_defaults(
            FakeUpdate(settings.telegram_authorized_chat_id),
            FakeContext(["set", "publish_youtube", "on"]),
        ) is True
        assert await bot.handle_defaults(
            FakeUpdate(settings.telegram_authorized_chat_id),
            FakeContext(["set", "publish_tiktok", "on"]),
        ) is True

        process_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(process_update, FakeContext(["process", "1"])) is True
        reply = process_update.message.replies[0]
        assert "https://clips.example.com/clips/" in reply
        assert "download" in reply
        assert "Title 1" in reply
        assert "Title 3" in reply
        assert "youtube: published https://youtube.example.com/" in reply
        assert "tiktok: published https://tiktok.example.com/" in reply
        assert processor.calls == [
            ("https://youtu.be/manual", 1, True, True),
            ("https://youtu.be/manual", 3, True, True),
        ]
        assert metadata_generator.calls == [
            ("https://youtu.be/manual", 1, "gemini-3.1-flash-lite"),
            ("https://youtu.be/manual", 3, "gemini-3.1-flash-lite"),
        ]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            run = session.get(RunLog, 1)
            assert run.status == "processed"
            clips = session.scalars(select(ClipRecord).order_by(ClipRecord.id)).all()
            assert len(clips) == 2
            assert [clip.generated_title for clip in clips] == ["Title 1", "Title 3"]
            assert [clip.generated_description for clip in clips] == ["Description 1", "Description 3"]
            assert all(clip.generated_hashtags == "#shorts #viral" for clip in clips)
            assert all(clip.public_clip_link in reply for clip in clips)
            attempts = session.scalars(select(PublishAttempt).order_by(PublishAttempt.id)).all()
            assert len(attempts) == 4
            assert {attempt.platform for attempt in attempts} == {"youtube", "tiktok"}
            assert all(attempt.status == "published" for attempt in attempts)

    asyncio.run(run_test())


def test_direct_video_url_selected_highlight_uses_local_source_path(monkeypatch, tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        processor = FakeDirectClipProcessor(settings.clip_archive_dir)
        service = ManualClippingService(
            settings,
            FakeDirectVideoHighlightFinder(),
            clip_processor=processor,
            metadata_generator=FakeMetadataGenerator(),
        )
        bot = AuthorizedOperatorTelegramBot(settings, manual_clipping_service=service)

        def fake_urlopen(request, timeout=30):
            return FakeHttpResponse(
                b"fake mp4 bytes",
                {"Content-Type": "video/mp4", "Content-Length": "14"},
            )

        monkeypatch.setattr("bot_app.manual_clipping.urlopen", fake_urlopen)

        direct_url = "https://media.example.com/videos/source.mp4"
        assert await bot.handle_clip(FakeUpdate(settings.telegram_authorized_chat_id), FakeContext([direct_url])) is True
        assert await bot.handle_clip(FakeUpdate(settings.telegram_authorized_chat_id), FakeContext(["select", "1", "1"])) is True

        process_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_clip(process_update, FakeContext(["process", "1"])) is True
        assert "https://clips.example.com/clips/" in process_update.message.replies[0]
        assert len(processor.calls) == 1
        assert processor.calls[0][0:4] == (direct_url, 1, True, True)

    asyncio.run(run_test())


def test_clipping_queue_allows_only_one_active_run(tmp_path):
    settings = make_settings(tmp_path)
    initialize_database(settings.database_url)
    processor = FakeClipProcessor(settings.clip_archive_dir)
    service = ManualClippingService(
        settings,
        FakeHighlightFinder(),
        clip_processor=processor,
        metadata_generator=FakeMetadataGenerator(),
    )
    service.clipping_queue.active_run_id = 99

    session_factory = create_session_factory(settings.database_url)
    with session_factory() as session:
        run = service.start_run(session, "https://youtu.be/manual")
        service.select_candidates(session, run.id, [1])
        links = service.process_selected_run(session, settings, run.id)

        assert links == []
        assert session.get(RunLog, run.id).status == "queued"
        assert processor.calls == []


def test_clip_rejects_unknown_chats_without_creating_run_logs(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        unauthorized_update = FakeUpdate(999)
        assert await bot.handle_clip(unauthorized_update, FakeContext(["https://youtu.be/nope"])) is False
        assert unauthorized_update.message.replies == ["Unauthorized chat."]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            assert session.scalars(select(RunLog)).all() == []

    asyncio.run(run_test())


def test_sources_command_displays_inline_controls_and_callbacks_mutate_queue(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)
        with create_session_factory(settings.database_url)() as session:
            add_source_videos(session, ["https://youtu.be/one"])

        sources_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_sources(sources_update, FakeContext()) is True
        assert "Source Video Queue" in sources_update.message.replies[0]
        callback_data = [button.callback_data for row in sources_update.message.reply_markups[0].inline_keyboard for button in row]
        assert "sources:add" in callback_data
        assert "sources:list" in callback_data
        assert "sources:remove:1" in callback_data

        prompt = FakeCallbackQuery("sources:add")
        assert await bot.handle_sources_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=prompt), FakeContext()) is True
        assert "/sources add" in prompt.edits[0]

        remove = FakeCallbackQuery("sources:remove:1")
        assert await bot.handle_sources_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=remove), FakeContext()) is True
        assert "cancelled" in remove.edits[0]

        unauthorized = FakeCallbackQuery("sources:list")
        assert await bot.handle_sources_callback(FakeUpdate(999, callback_query=unauthorized), FakeContext()) is False
        assert unauthorized.answers == ["Unauthorized chat."]

    asyncio.run(run_test())


def test_authorized_operator_can_add_list_and_remove_source_videos(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        add_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_sources(
            add_update,
            FakeContext(["add", "https://youtu.be/one", "https://youtu.be/two"]),
        ) is True
        assert "Added 2 Source Videos" in add_update.message.replies[0]

        list_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_sources(list_update, FakeContext(["list"])) is True
        assert "pending" in list_update.message.replies[0]
        assert "https://youtu.be/one" in list_update.message.replies[0]
        assert "https://youtu.be/two" in list_update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            pending = get_pending_source_videos(session)
            source_id = pending[0].id

        remove_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_sources(remove_update, FakeContext(["remove", str(source_id)])) is True
        assert f"Removed Source Video {source_id}" in remove_update.message.replies[0]

        updated_list = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_sources(updated_list, FakeContext(["list"])) is True
        assert "cancelled" in updated_list.message.replies[0]

    asyncio.run(run_test())


def test_source_queue_consumes_pending_videos_once_and_rejects_unknown_chats(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        unauthorized_update = FakeUpdate(999)
        assert await bot.handle_sources(unauthorized_update, FakeContext(["add", "https://youtu.be/nope"])) is False
        assert unauthorized_update.message.replies == ["Unauthorized chat."]

        add_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_sources(add_update, FakeContext(["add", "https://youtu.be/one"])) is True

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            pending = get_pending_source_videos(session)
            assert len(pending) == 1
            consume_source_video(session, pending[0])
            assert get_pending_source_videos(session) == []

        list_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_sources(list_update, FakeContext(["list"])) is True
        assert "consumed" in list_update.message.replies[0]

    asyncio.run(run_test())


def test_schedule_command_displays_inline_controls_and_callbacks_mutate_slots(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)
        with create_session_factory(settings.database_url)() as session:
            add_daily_schedule(session, settings, "09:00")

        schedule_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_schedule(schedule_update, FakeContext()) is True
        assert "Schedules:" in schedule_update.message.replies[0]
        callback_data = [button.callback_data for row in schedule_update.message.reply_markups[0].inline_keyboard for button in row]
        assert "schedule:add_daily" in callback_data
        assert "schedule:add_weekly" in callback_data
        assert "schedule:list" in callback_data
        assert "schedule:disable:1" in callback_data
        assert "schedule:delete:1" in callback_data

        prompt = FakeCallbackQuery("schedule:add_daily")
        assert await bot.handle_schedule_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=prompt), FakeContext()) is True
        assert "/schedule add daily" in prompt.edits[0]

        disable = FakeCallbackQuery("schedule:disable:1")
        assert await bot.handle_schedule_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=disable), FakeContext()) is True
        assert "disabled" in disable.edits[0]

        enable = FakeCallbackQuery("schedule:enable:1")
        assert await bot.handle_schedule_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=enable), FakeContext()) is True
        assert "enabled" in enable.edits[0]

        delete = FakeCallbackQuery("schedule:delete:1")
        assert await bot.handle_schedule_callback(FakeUpdate(settings.telegram_authorized_chat_id, callback_query=delete), FakeContext()) is True
        assert "No schedules configured" in delete.edits[0]

        unauthorized = FakeCallbackQuery("schedule:list")
        assert await bot.handle_schedule_callback(FakeUpdate(999, callback_query=unauthorized), FakeContext()) is False
        assert unauthorized.answers == ["Unauthorized chat."]

    asyncio.run(run_test())


def test_authorized_operator_can_add_list_and_remove_schedule_slots(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        daily_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_schedule(daily_update, FakeContext(["add", "daily", "09:30"])) is True
        assert "Added daily schedule" in daily_update.message.replies[0]
        assert "Asia/Jakarta" in daily_update.message.replies[0]

        weekly_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_schedule(weekly_update, FakeContext(["add", "weekly", "monday", "10:15"])) is True
        assert "Added weekly schedule" in weekly_update.message.replies[0]

        list_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_schedule(list_update, FakeContext(["list"])) is True
        assert "daily 09:30 Asia/Jakarta enabled" in list_update.message.replies[0]
        assert "weekly monday 10:15 Asia/Jakarta enabled" in list_update.message.replies[0]

        remove_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_schedule(remove_update, FakeContext(["remove", "1"])) is True
        assert "Removed schedule 1" in remove_update.message.replies[0]

    asyncio.run(run_test())


def test_scheduled_firing_consumes_one_source_video_and_empty_queue_is_silent(tmp_path):
    settings = make_settings(tmp_path)
    initialize_database(settings.database_url)
    session_factory = create_session_factory(settings.database_url)

    with session_factory() as session:
        from bot_app.source_queue import add_source_videos

        add_source_videos(session, ["https://youtu.be/one", "https://youtu.be/two"])
        result = fire_schedule(session, settings, schedule_id=1)
        assert result.message_telegram is True
        assert result.source_url == "https://youtu.be/one"
        assert len(get_pending_source_videos(session)) == 1

        empty_source = get_pending_source_videos(session)[0]
        consume_source_video(session, empty_source)
        empty_result = fire_schedule(session, settings, schedule_id=1)
        assert empty_result.message_telegram is False
        assert empty_result.source_url is None
        assert len(session.scalars(select(RunLog)).all()) == 2


def test_authorized_operator_can_view_and_update_workflow_defaults(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        view_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_defaults(view_update, FakeContext()) is True
        assert "captions: on" in view_update.message.replies[0]
        assert "hooks: on" in view_update.message.replies[0]

        updates = [
            (["set", "captions", "off"], "captions: off"),
            (["set", "hooks", "off"], "hooks: off"),
            (["set", "publish_youtube", "on"], "publish_youtube: on"),
            (["set", "publish_tiktok", "on"], "publish_tiktok: on"),
            (["set", "subtitle_language", "id"], "subtitle_language: id"),
        ]
        for args, confirmation in updates:
            update = FakeUpdate(settings.telegram_authorized_chat_id)
            assert await bot.handle_defaults(update, FakeContext(args)) is True
            assert confirmation in update.message.replies[0]

        updated_view = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_defaults(updated_view, FakeContext()) is True
        reply = updated_view.message.replies[0]
        assert "captions: off" in reply
        assert "hooks: off" in reply
        assert "publish_youtube: on" in reply
        assert "publish_tiktok: on" in reply
        assert "subtitle_language: id" in reply

    asyncio.run(run_test())


def test_defaults_rejects_unknown_chats_and_invalid_updates_without_mutating(tmp_path):
    async def run_test():
        settings = make_settings(tmp_path)
        initialize_database(settings.database_url)
        bot = AuthorizedOperatorTelegramBot(settings)

        unauthorized_update = FakeUpdate(999)
        assert await bot.handle_defaults(unauthorized_update, FakeContext(["set", "captions", "off"])) is False
        assert unauthorized_update.message.replies == ["Unauthorized chat."]

        invalid_update = FakeUpdate(settings.telegram_authorized_chat_id)
        assert await bot.handle_defaults(invalid_update, FakeContext(["set", "captions", "maybe"])) is False
        assert "Usage:" in invalid_update.message.replies[0]

        session_factory = create_session_factory(settings.database_url)
        with session_factory() as session:
            defaults = session.scalars(select(WorkflowDefaults)).one()
            assert defaults.captions_enabled is True

    asyncio.run(run_test())


def test_telegram_bot_startup_fails_fast_without_required_configuration(tmp_path):
    settings = make_settings(tmp_path)
    settings.telegram_bot_token = ""

    bot = AuthorizedOperatorTelegramBot(settings)

    try:
        bot.validate_configuration()
    except ValueError as exc:
        assert "TELEGRAM_BOT_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected missing Telegram configuration to fail fast")


def test_gemini_text_provider_uses_environment_configuration(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return type("Response", (), {"text": "highlight json"})()

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            self.models = FakeModels()

    monkeypatch.setattr("bot_app.ai_providers.genai.Client", FakeClient)

    provider = GeminiTextProvider(settings)
    result = provider.generate_text("find highlights")

    assert provider.client.api_key == "gemini-test-key"
    assert result == "highlight json"
    assert calls == [{"model": "gemini-3.1-flash-lite", "contents": "find highlights"}]


def test_openrouter_audio_adapter_points_openai_sdk_at_openrouter(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    captured_client_kwargs = {}
    transcription_calls = []
    speech_calls = []

    class FakeTranscriptions:
        def create(self, **kwargs):
            transcription_calls.append(kwargs)
            return {"text": "caption"}

    class FakeSpeech:
        def create(self, **kwargs):
            speech_calls.append(kwargs)
            return type("SpeechResponse", (), {"content": b"audio"})()

    class FakeAudio:
        def __init__(self):
            self.transcriptions = FakeTranscriptions()
            self.speech = FakeSpeech()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_client_kwargs.update(kwargs)
            self.audio = FakeAudio()

    monkeypatch.setattr("bot_app.ai_providers.OpenAI", FakeOpenAI)

    audio_file = object()
    adapter = OpenRouterAudioAdapter(settings)
    transcript = adapter.transcribe(file=audio_file)
    speech = adapter.create_hook_voice("listen now")

    assert captured_client_kwargs == {
        "api_key": "openrouter-test-key",
        "base_url": "https://openrouter.ai/api/v1",
    }
    assert transcript == {"text": "caption"}
    assert speech.content == b"audio"
    assert transcription_calls == [{"model": "openai/whisper-1", "file": audio_file}]
    assert speech_calls == [
        {
            "model": "canopylabs/orpheus-3b-0.1-ft",
            "voice": "josh",
            "input": "listen now",
        }
    ]


def test_core_transcription_uses_openrouter_json_audio_payload(monkeypatch, tmp_path):
    audio_file = tmp_path / "audio.mp3"
    audio_file.write_bytes(b"mp3-bytes")
    posts = []

    class FakeResponse:
        text = '{"text":"hello world"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"text": "hello world"}

    def fake_post(url, headers=None, json=None, data=None, files=None, timeout=None):
        posts.append({"url": url, "headers": headers, "json": json, "data": data, "files": files, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)

    core = AutoClipperCore.__new__(AutoClipperCore)
    core.caption_client = type("CaptionClient", (), {"base_url": "https://openrouter.ai/api/v1", "api_key": "test-key"})()
    core.whisper_model = "openai/whisper-1"
    core.subtitle_language = "en"
    core.ffmpeg_path = "/no/ffmpeg"
    core.log = lambda *_args, **_kwargs: None
    core.set_progress = lambda *_args, **_kwargs: None
    core.is_cancelled = lambda: False

    segments = core._whisper_transcribe_file(str(audio_file), 5)

    assert segments == [{"start": 5, "end": 35, "text": "hello world"}]
    assert posts[0]["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert posts[0]["headers"]["Content-Type"] == "application/json"
    assert posts[0]["json"]["model"] == "openai/whisper-1"
    assert posts[0]["json"]["input_audio"]["format"] == "mp3"
    assert posts[0]["json"]["input_audio"]["data"]
    assert posts[0]["data"] is None
    assert posts[0]["files"] is None


def test_environment_configuration_does_not_require_openai_provider_settings(tmp_path):
    settings = make_settings(tmp_path)

    assert not hasattr(settings, "openai_api_key")
    assert not hasattr(settings, "ai_providers")


def test_environment_configuration_uses_bot_control_mode_defaults(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.app_timezone == "Asia/Jakarta"
    assert settings.clip_retention_days == 30
    assert settings.gemini_highlight_model == "gemini-3.1-flash-lite"
    assert settings.gemini_youtube_title_model == "gemini-3.1-flash-lite"
    assert settings.openrouter_transcription_model == "openai/whisper-1"
    assert settings.openrouter_tts_model == "canopylabs/orpheus-3b-0.1-ft"
    assert settings.openrouter_tts_voice == "josh"


def test_combined_bot_server_creates_sqlite_parent_directory(tmp_path):
    settings = make_settings(tmp_path)
    nested_database_url = f"sqlite:///{tmp_path / 'nested' / 'data' / 'bot.db'}"

    initialize_database(nested_database_url)

    assert (tmp_path / "nested" / "data" / "bot.db").exists()


def test_clip_archive_cleanup_removes_expired_files_and_preserves_run_logs(tmp_path):
    settings = make_settings(tmp_path)
    initialize_database(settings.database_url)
    expired_file = settings.clip_archive_dir / "expired.mp4"
    active_file = settings.clip_archive_dir / "active.mp4"
    outside_file = tmp_path / "outside.mp4"
    expired_file.parent.mkdir(parents=True, exist_ok=True)
    expired_file.write_bytes(b"expired")
    active_file.write_bytes(b"active")
    outside_file.write_bytes(b"outside")

    session_factory = create_session_factory(settings.database_url)
    with session_factory() as session:
        session.add(RunLog(source_url="https://youtu.be/history", status="processed"))
        expired = create_clip_record(
            session,
            settings,
            expired_file,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        active = create_clip_record(session, settings, active_file)
        unsafe = create_clip_record(
            session,
            settings,
            outside_file,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

        deleted_count = cleanup_expired_clips(session, settings)

        assert deleted_count == 1
        assert not expired_file.exists()
        assert active_file.exists()
        assert outside_file.exists()
        assert session.get(ClipRecord, expired.id).deleted_at is not None
        assert session.get(ClipRecord, active.id).deleted_at is None
        assert session.get(ClipRecord, unsafe.id).deleted_at is None
        assert session.scalars(select(RunLog)).one().source_url == "https://youtu.be/history"


def test_combined_bot_server_runs_clip_archive_cleanup_on_startup(tmp_path):
    settings = make_settings(tmp_path)
    initialize_database(settings.database_url)
    expired_file = settings.clip_archive_dir / "expired.mp4"
    expired_file.parent.mkdir(parents=True, exist_ok=True)
    expired_file.write_bytes(b"expired")

    session_factory = create_session_factory(settings.database_url)
    with session_factory() as session:
        create_clip_record(
            session,
            settings,
            expired_file,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

    app = create_app(settings, telegram_bot=FakeTelegramBot())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert not expired_file.exists()


def test_public_clip_link_downloads_recorded_archive_file(tmp_path):
    settings = make_settings(tmp_path)
    clip_file = settings.clip_archive_dir / "session-1" / "master.mp4"
    clip_file.parent.mkdir(parents=True)
    clip_file.write_bytes(b"clip-bytes")

    initialize_database(settings.database_url)
    session_factory = create_session_factory(settings.database_url)
    with session_factory() as session:
        clip = create_clip_record(session, settings, clip_file)
        assert clip.public_clip_link == f"https://clips.example.com/clips/{clip.clip_id}/download"
        assert clip.expires_at is not None
        assert clip.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)

    app = create_app(settings, telegram_bot=FakeTelegramBot())
    with TestClient(app) as client:
        response = client.get(f"/clips/{clip.clip_id}/download")

    assert response.status_code == 200
    assert response.content == b"clip-bytes"


def test_public_clip_link_rejects_unknown_missing_expired_deleted_and_unsafe_records(tmp_path):
    settings = make_settings(tmp_path)
    initialize_database(settings.database_url)
    session_factory = create_session_factory(settings.database_url)

    missing_file = settings.clip_archive_dir / "missing.mp4"
    expired_file = settings.clip_archive_dir / "expired.mp4"
    deleted_file = settings.clip_archive_dir / "deleted.mp4"
    unsafe_file = tmp_path / "outside.mp4"
    expired_file.parent.mkdir(parents=True)
    expired_file.write_bytes(b"expired")
    deleted_file.write_bytes(b"deleted")
    unsafe_file.write_bytes(b"unsafe")

    with session_factory() as session:
        missing = create_clip_record(session, settings, missing_file)
        expired = create_clip_record(
            session,
            settings,
            expired_file,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        deleted = create_clip_record(session, settings, deleted_file, deleted_at=datetime.now(timezone.utc))
        unsafe = create_clip_record(session, settings, unsafe_file)

    app = create_app(settings, telegram_bot=FakeTelegramBot())
    with TestClient(app) as client:
        assert client.get("/clips/unknown/download").status_code == 404
        assert client.get(f"/clips/{missing.clip_id}/download").status_code == 404
        assert client.get(f"/clips/{expired.clip_id}/download").status_code == 404
        assert client.get(f"/clips/{deleted.clip_id}/download").status_code == 404
        assert client.get(f"/clips/{unsafe.clip_id}/download").status_code == 404
        assert client.get("/clips/../download").status_code in {404, 422}


def test_combined_bot_server_reports_health_and_creates_workflow_defaults(tmp_path):
    settings = make_settings(tmp_path)

    app = create_app(settings, telegram_bot=FakeTelegramBot())
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    session_factory = create_session_factory(settings.database_url)
    with session_factory() as session:
        defaults = session.scalars(select(WorkflowDefaults)).one()
        assert defaults.captions_enabled is True
        assert defaults.hooks_enabled is True
        assert defaults.publish_youtube is False
        assert defaults.publish_tiktok is False
        assert defaults.subtitle_language == "en"
        assert defaults.manual_highlight_candidates == 5
        assert defaults.scheduled_highlight_candidates == 5
        assert defaults.scheduled_clips_per_source == 1
        assert defaults.scheduled_source_videos_per_run == 1

    initialize_database(settings.database_url)

    with session_factory() as session:
        assert len(session.scalars(select(WorkflowDefaults)).all()) == 1
