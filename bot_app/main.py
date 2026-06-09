from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from bot_app.clip_archive import resolve_downloadable_clip_path
from bot_app.database import create_session_factory, initialize_database
from bot_app.models import ClipRecord
from bot_app.settings import Settings
from bot_app.telegram_bot import AuthorizedOperatorTelegramBot, TelegramBotShell


def create_app(settings: Settings | None = None, telegram_bot: TelegramBotShell | None = None) -> FastAPI:
    app_settings = settings or Settings()
    bot = telegram_bot or AuthorizedOperatorTelegramBot(app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = app_settings
        app.state.session_factory = create_session_factory(app_settings.database_url)
        initialize_database(app_settings.database_url)
        await bot.start()
        try:
            yield
        finally:
            await bot.stop()

    app = FastAPI(title="YT Short Clipper Bot Control Mode", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/clips/{clip_id}/download")
    async def download_clip(clip_id: str) -> FileResponse:
        with app.state.session_factory() as session:
            record = session.scalars(select(ClipRecord).where(ClipRecord.clip_id == clip_id)).first()
            if record is None:
                raise HTTPException(status_code=404, detail="Clip not found")

            clip_path = resolve_downloadable_clip_path(record, app.state.settings)
            if clip_path is None:
                raise HTTPException(status_code=404, detail="Clip not found")

            return FileResponse(clip_path, media_type="video/mp4", filename=clip_path.name)

    return app
