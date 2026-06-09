from contextlib import asynccontextmanager

from fastapi import FastAPI

from bot_app.database import initialize_database
from bot_app.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = app_settings
        initialize_database(app_settings.database_url)
        yield

    app = FastAPI(title="YT Short Clipper Bot Control Mode", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
