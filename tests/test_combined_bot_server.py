import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from bot_app.ai_providers import GeminiTextProvider, OpenRouterAudioAdapter
from bot_app.clip_archive import create_clip_record
from bot_app.manual_clipping import HighlightDraft, ManualClippingService
from bot_app.database import create_session_factory, initialize_database
from bot_app.main import create_app
from bot_app.models import HighlightCandidate, RunEvent, RunLog, WorkflowDefaults
from bot_app.source_queue import consume_source_video, get_pending_source_videos
from bot_app.settings import Settings
from bot_app.telegram_bot import AuthorizedOperatorTelegramBot


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="https://clips.example.com",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
        telegram_bot_token="123:test-token",
        telegram_authorized_chat_id=123456,
        gemini_api_key="gemini-test-key",
        openrouter_api_key="openrouter-test-key",
        clip_archive_dir=tmp_path / "clips",
    )


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeUpdate:
    def __init__(self, chat_id):
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


class FakeTelegramBot:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def test_fastapi_lifespan_starts_and_stops_telegram_bot(tmp_path):
    settings = make_settings(tmp_path)
    telegram_bot = FakeTelegramBot()

    app = create_app(settings, telegram_bot=telegram_bot)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert telegram_bot.started is True
        assert telegram_bot.stopped is False

    assert telegram_bot.stopped is True


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


class FakeHighlightFinder:
    def __init__(self):
        self.calls = []

    def find_highlights(self, youtube_url, count):
        self.calls.append((youtube_url, count))
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
        assert finder.calls == [("https://youtu.be/manual", 5)]

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

    app = create_app(settings)
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

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/clips/unknown/download").status_code == 404
        assert client.get(f"/clips/{missing.clip_id}/download").status_code == 404
        assert client.get(f"/clips/{expired.clip_id}/download").status_code == 404
        assert client.get(f"/clips/{deleted.clip_id}/download").status_code == 404
        assert client.get(f"/clips/{unsafe.clip_id}/download").status_code == 404
        assert client.get("/clips/../download").status_code in {404, 422}


def test_combined_bot_server_reports_health_and_creates_workflow_defaults(tmp_path):
    settings = make_settings(tmp_path)

    app = create_app(settings)
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
