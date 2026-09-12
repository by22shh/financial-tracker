"""Загрузка вложений Telegram с ограничениями (раздел 19.4 ТЗ, ADR-11).

Ссылки на файлы с токеном бота не логируются. Облачный getFile ограничен
20 MB; продуктовый предел P0 — 15 MB.
"""

from __future__ import annotations

import httpx

from fintracker.config import Settings
from fintracker.core.errors import ProviderUnavailable, ValidationFailed
from fintracker.core.logging import get_logger

logger = get_logger("telegram.files")

TELEGRAM_CLOUD_LIMIT = 20 * 1024 * 1024


async def download_attachment(settings: Settings, *, file_id: str) -> bytes:
    """Скачать файл по file_id, не раскрывая токен в логах."""
    token = settings.telegram.bot_token.get_secret_value()
    if not token:
        raise ProviderUnavailable("Токен Telegram не настроен (BL-03)")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            info = await client.get(
                f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}
            )
        except httpx.TransportError as exc:
            raise ProviderUnavailable("Telegram недоступен") from exc
        body = info.json()
        if not body.get("ok"):
            raise ProviderUnavailable("Файл недоступен в Telegram")
        result = body["result"]
        size = int(result.get("file_size") or 0)
        if size > settings.limits.max_attachment_bytes:
            raise ValidationFailed("Файл превышает допустимый размер вложения")
        if size > TELEGRAM_CLOUD_LIMIT:
            raise ValidationFailed("Файл превышает облачный предел Telegram")
        path = result["file_path"]
        try:
            response = await client.get(f"https://api.telegram.org/file/bot{token}/{path}")
        except httpx.TransportError as exc:
            raise ProviderUnavailable("Не удалось скачать файл") from exc
        if response.status_code != 200:
            raise ProviderUnavailable("Не удалось скачать файл")
        data = response.content
        if len(data) > settings.limits.max_attachment_bytes:
            raise ValidationFailed("Файл превышает допустимый размер вложения")
        logger.info("attachment_downloaded", size_bytes=len(data))
        return data
