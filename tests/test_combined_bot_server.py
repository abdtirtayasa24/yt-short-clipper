from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

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
