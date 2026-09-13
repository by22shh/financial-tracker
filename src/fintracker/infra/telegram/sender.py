"""Отправка сообщений с учётом ограничений Telegram (раздел 19.4 ТЗ, AR-30).

Гарантия однократности относится к финансовой записи, а не к транспорту:
при неопределённом результате ``sendMessage`` возможен дубликат уведомления.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from fintracker.config import Settings
from fintracker.core.logging import get_logger

logger = get_logger("telegram.sender")

MAX_MESSAGE_CHARS = 4096


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    message_id: int | None = None
    error: str | None = None
    retry_after: float | None = None
    blocked: bool = False
    unknown: bool = False


class TelegramSender(Protocol):
    async def send_message(
        self, *, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> SendResult: ...

    async def send_document(
        self, *, chat_id: int, filename: str, content: bytes, caption: str | None = None
    ) -> SendResult: ...


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Длинные ответы разбиваются с сохранением строк (раздел 19.4)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and current:
            parts.append("".join(current))
            current, size = [], 0
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current.append(line)
        size += len(line)
    if current:
        parts.append("".join(current))
    return parts


class RateLimiter:
    """Очередь на чат и на бот; 429 обрабатывается по retry_after."""

    def __init__(self, *, per_chat_per_second: float, global_per_second: float) -> None:
        self._chat_interval = 1.0 / max(per_chat_per_second, 0.01)
        self._global_interval = 1.0 / max(global_per_second, 0.01)
        self._last_chat: dict[int, float] = {}
        self._last_global = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, chat_id: int) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_global = max(0.0, self._last_global + self._global_interval - now)
            wait_chat = max(0.0, self._last_chat.get(chat_id, 0.0) + self._chat_interval - now)
            delay = max(wait_global, wait_chat)
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.monotonic()
            self._last_global = now
            self._last_chat[chat_id] = now


class HttpTelegramSender:
    """Реальный клиент Bot API."""

    def __init__(self, settings: Settings) -> None:
        self._token = settings.telegram.bot_token.get_secret_value()
        self._limiter = RateLimiter(
            per_chat_per_second=settings.telegram.send_rate_per_chat_per_second,
            global_per_second=settings.telegram.send_rate_global_per_second,
        )

    async def send_message(
        self, *, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> SendResult:
        chunks = split_message(text)
        last: SendResult = SendResult(ok=False, error="Пустое сообщение")
        async with httpx.AsyncClient(timeout=20.0) as client:
            for index, chunk in enumerate(chunks):
                await self._limiter.acquire(chat_id)
                payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
                if buttons and index == len(chunks) - 1:
                    payload["reply_markup"] = {"inline_keyboard": buttons}
                try:
                    response = await client.post(
                        f"https://api.telegram.org/bot{self._token}/sendMessage", json=payload
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    # Неопределённый результат: сообщение могло быть доставлено.
                    return SendResult(ok=False, unknown=True, error=f"{type(exc).__name__}")
                if response.status_code == 429:
                    retry_after = float(response.json().get("parameters", {}).get("retry_after", 1))
                    return SendResult(ok=False, error="rate_limited", retry_after=retry_after)
                body = response.json()
                if not body.get("ok"):
                    description = str(body.get("description", ""))
                    blocked = (
                        "bot was blocked" in description or "user is deactivated" in description
                    )
                    return SendResult(ok=False, error=description[:300], blocked=blocked)
                last = SendResult(ok=True, message_id=body["result"]["message_id"])
        return last

    async def send_document(
        self, *, chat_id: int, filename: str, content: bytes, caption: str | None = None
    ) -> SendResult:
        """Выдать файл выгрузки получателю (FR-67, CMD-29)."""
        await self._limiter.acquire(chat_id)
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{self._token}/sendDocument",
                    data=data,
                    files={"document": (filename, content)},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                return SendResult(ok=False, unknown=True, error=f"{type(exc).__name__}")
            if response.status_code == 429:
                retry_after = float(response.json().get("parameters", {}).get("retry_after", 1))
                return SendResult(ok=False, error="rate_limited", retry_after=retry_after)
            body = response.json()
            if not body.get("ok"):
                description = str(body.get("description", ""))
                blocked = "bot was blocked" in description or "user is deactivated" in description
                return SendResult(ok=False, error=description[:300], blocked=blocked)
            return SendResult(ok=True, message_id=body["result"]["message_id"])


@dataclass
class RecordingSender:
    """Контролируемый отправитель для проверок без реального Telegram."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    fail_for_chats: set[int] = field(default_factory=set)
    blocked_chats: set[int] = field(default_factory=set)
    unknown_chats: set[int] = field(default_factory=set)
    _next_id: int = 1

    async def send_message(
        self, *, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> SendResult:
        if chat_id in self.blocked_chats:
            return SendResult(ok=False, blocked=True, error="bot was blocked by the user")
        if chat_id in self.unknown_chats:
            return SendResult(ok=False, unknown=True, error="timeout")
        if chat_id in self.fail_for_chats:
            return SendResult(ok=False, error="temporary failure", retry_after=1)
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        self._next_id += 1
        return SendResult(ok=True, message_id=self._next_id)

    async def send_document(
        self, *, chat_id: int, filename: str, content: bytes, caption: str | None = None
    ) -> SendResult:
        if chat_id in self.blocked_chats:
            return SendResult(ok=False, blocked=True, error="bot was blocked by the user")
        if chat_id in self.fail_for_chats:
            return SendResult(ok=False, error="temporary failure", retry_after=1)
        self.documents.append(
            {"chat_id": chat_id, "filename": filename, "size": len(content), "caption": caption}
        )
        self._next_id += 1
        return SendResult(ok=True, message_id=self._next_id)


_OVERRIDE: TelegramSender | None = None


def set_sender_override(sender: TelegramSender | None) -> None:
    """Подменить отправителя в проверках (моки не доказывают доступность API)."""
    global _OVERRIDE
    _OVERRIDE = sender


def build_sender(settings: Settings) -> TelegramSender:
    if _OVERRIDE is not None:
        return _OVERRIDE
    if not settings.telegram.configured:
        logger.warning("telegram_not_configured")
        return RecordingSender()
    return HttpTelegramSender(settings)
