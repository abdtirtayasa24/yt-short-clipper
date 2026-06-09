from typing import Any, BinaryIO

from google import genai
from openai import OpenAI

from bot_app.settings import Settings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class GeminiTextProvider:
    """Gemini Text Provider for Bot Control Mode text-generation tasks."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = genai.Client(api_key=settings.gemini_api_key)

    def generate_text(self, prompt: str, model: str | None = None) -> str:
        response = self.client.models.generate_content(
            model=model or self.settings.gemini_highlight_model,
            contents=prompt,
        )
        return response.text or ""


class OpenRouterAudioAdapter:
    """OpenAI SDK compatibility adapter pointed only at OpenRouter audio endpoints."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
        )

    def transcribe(self, file: BinaryIO, model: str | None = None) -> Any:
        return self.client.audio.transcriptions.create(
            model=model or self.settings.openrouter_transcription_model,
            file=file,
        )

    def create_hook_voice(
        self,
        text: str,
        model: str | None = None,
        voice: str | None = None,
    ) -> Any:
        return self.client.audio.speech.create(
            model=model or self.settings.openrouter_tts_model,
            voice=voice or self.settings.openrouter_tts_voice,
            input=text,
        )
