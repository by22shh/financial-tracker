"""Приём Telegram webhook (CMD-01, TECH-01)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response, status

from fintracker.core.errors import Unauthenticated, ValidationFailed

router = APIRouter(tags=["telegram"])

# Ограничение размера тела до разбора (TECH-01, NFR-11).
MAX_UPDATE_BYTES = 1024 * 1024


@router.post("/telegram/webhook", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    """Проверить секрет, сохранить событие и задачу до ответа 2xx.

    Распознавание не выполняется внутри ожидания webhook (TECH-01).
    """
    from fintracker.application.ingestion.accept_update import accept_telegram_update

    settings = request.app.state.settings
    expected = settings.telegram.webhook_secret.get_secret_value()
    if not expected:
        raise Unauthenticated("Webhook не настроен")
    if x_telegram_bot_api_secret_token != expected:
        raise Unauthenticated("Неверный секрет webhook")

    raw = await request.body()
    if len(raw) > MAX_UPDATE_BYTES:
        raise ValidationFailed("Слишком большое обновление")
    try:
        payload: dict[str, Any] = await request.json()
    except ValueError as exc:
        raise ValidationFailed("Некорректное тело обновления") from exc

    await accept_telegram_update(settings, payload)
    return Response(status_code=status.HTTP_200_OK)
