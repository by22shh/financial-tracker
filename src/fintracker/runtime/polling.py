"""Long-polling режим для локальной проверки без публичного HTTPS (BL-03).

Приём остаётся долговечным: обновление сохраняется в inbox и обрабатывается
той же очередью, что и webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

import httpx

from fintracker.application.ingestion.accept_update import accept_telegram_update, callback_ack
from fintracker.config import Settings
from fintracker.core.logging import get_logger

logger = get_logger("runtime.polling")

POLL_TIMEOUT_SECONDS = 25


async def run_polling(settings: Settings, *, stop_event: asyncio.Event | None = None) -> None:
    token = settings.telegram.bot_token.get_secret_value()
    if not token:
        raise RuntimeError("Для long-polling нужен FINTRACKER_TELEGRAM__BOT_TOKEN (BL-03)")
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    offset: int | None = None
    logger.info("polling_started")
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS + 10) as client:
        while not stop.is_set():
            params: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS}
            if offset is not None:
                params["offset"] = offset
            try:
                response = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates", params=params
                )
            except httpx.TransportError:
                await asyncio.sleep(2)
                continue
            body = response.json()
            if not body.get("ok"):
                logger.warning("polling_error", description=body.get("description"))
                await asyncio.sleep(2)
                continue
            for item in body.get("result", []):
                await accept_telegram_update(settings, item)
                offset = int(item["update_id"]) + 1
                ack = callback_ack(item)
                if ack is not None:
                    # В режиме опроса нажатие подтверждается отдельным вызовом.
                    with contextlib.suppress(httpx.HTTPError):
                        await client.post(
                            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                            json={"callback_query_id": ack["callback_query_id"]},
                        )
    logger.info("polling_stopped")
