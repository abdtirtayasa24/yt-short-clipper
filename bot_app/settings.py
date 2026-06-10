from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment Configuration for Bot Control Mode."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    public_base_url: AnyHttpUrl
    database_url: str = "sqlite:///data/app.db"
    app_timezone: str = "Asia/Jakarta"
    clip_archive_dir: Path = Path("clip_archive")
    clip_retention_days: int = Field(default=30, ge=1)
    youtube_credentials_path: Path = Path("credentials/youtube.json")
    tiktok_session_path: Path = Path("credentials/tiktok.session")

    telegram_bot_token: str
    telegram_authorized_chat_id: int

    gemini_api_key: str
    gemini_highlight_model: str = "gemini-3.1-flash-lite"
    gemini_youtube_title_model: str = "gemini-3.1-flash-lite"

    openrouter_api_key: str
    openrouter_transcription_model: str = "openai/whisper-1"
    openrouter_tts_model: str = "canopylabs/orpheus-3b-0.1-ft"
    openrouter_tts_voice: str = "josh"

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
        return value
