"""FastAPI приложение: health, readiness и маршруты /v1 (ADR-10, ADR-13)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from fintracker.api.errors import domain_error_handler, unexpected_error_handler
from fintracker.config import Settings, get_settings
from fintracker.core.errors import DomainError
from fintracker.core.logging import configure_logging
from fintracker.db.session import dispose_engines
from fintracker.runtime.health import check_readiness


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engines()


def create_app(settings: Settings | None = None) -> FastAPI:
    active = settings or get_settings()
    configure_logging(active.observability)
    app = FastAPI(
        title="Финансовый трекер",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if active.env != "prod" else None,
        openapi_url="/openapi.json",
    )
    app.state.settings = active
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)

    @app.get("/health/live", tags=["operations"])
    async def live() -> dict[str, Any]:
        """Liveness: процесс отвечает (ADR-13)."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["operations"])
    async def ready() -> JSONResponse:
        """Readiness: база доступна и схема совместима (OPS-02)."""
        report = await check_readiness(app.state.settings)
        return JSONResponse(status_code=200 if report.ready else 503, content=report.to_payload())

    from fintracker.api.routes import register_routes

    register_routes(app)
    return app
