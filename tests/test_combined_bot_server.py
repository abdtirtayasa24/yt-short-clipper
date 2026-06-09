from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from bot_app.ai_providers import GeminiTextProvider, OpenRouterAudioAdapter
from bot_app.clip_archive import create_clip_record
from bot_app.database import create_session_factory, initialize_database
from bot_app.main import create_app
from bot_app.models import WorkflowDefaults
from bot_app.settings import Settings


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
