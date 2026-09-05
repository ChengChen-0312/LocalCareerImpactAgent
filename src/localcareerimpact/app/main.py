"""Application entry point for the local MVP backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from localcareerimpact.knowledge import KnowledgeService
from localcareerimpact.workers.client import WorkerStartupError

from .api import router
from .auth import CsvCredentialStore, SessionStore
from .chat_store import ChatStore
from .config import AppSettings, ConfigurationError
from .database import Database
from .model_runtime import ModelRuntime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    model_runtime: ModelRuntime | None = None
    try:
        settings = AppSettings.from_repository_root()
        settings.validate_users_csv()
        settings.load_model_paths()
        credential_store = CsvCredentialStore(settings.users_csv_path)
        credential_store.validate_configuration()
        settings.upload_root.mkdir(parents=True, exist_ok=True)
        settings.knowledge_inbox.mkdir(parents=True, exist_ok=True)
        database = Database(settings.database_path)
        database.initialize()
        model_runtime = ModelRuntime(settings)
        app.state.model_runtime = model_runtime
        await model_runtime.start()
        knowledge_service = KnowledgeService(database, settings, model_runtime)
    except ConfigurationError as exc:
        raise RuntimeError(f"LocalCareerImpactAgent startup configuration error: {exc}") from exc
    except WorkerStartupError as exc:
        raise RuntimeError(
            f"LocalCareerImpactAgent {exc.model_key} worker readiness error: {exc.reason}"
        ) from exc

    app.state.settings = settings
    app.state.credential_store = credential_store
    app.state.session_store = SessionStore()
    app.state.database = database
    app.state.chat_store = ChatStore(database)
    app.state.knowledge_service = knowledge_service
    try:
        yield
    finally:
        if model_runtime is not None:
            await model_runtime.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="LocalCareerImpactAgent MVP",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_web_app(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        settings: AppSettings = app.state.settings
        web_dist = settings.database_path.parents[1] / "web" / "dist"
        requested_path = (web_dist / full_path).resolve()
        if full_path and requested_path.is_relative_to(web_dist.resolve()) and requested_path.is_file():
            return FileResponse(requested_path)
        return FileResponse(web_dist / "index.html")

    return app


app = create_app()
