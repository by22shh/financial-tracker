"""Регистрация маршрутов /v1 (DATA_CONTRACT §4)."""

from __future__ import annotations

from fastapi import FastAPI


def register_routes(app: FastAPI) -> None:
    from fintracker.api.routes import telegram

    app.include_router(telegram.router, prefix="/v1")
