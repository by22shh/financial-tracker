"""Фикстуры сквозных сценариев через разговорный слой.

Проверяется полный пользовательский путь без реального Telegram; отдельная
проверка на живых клиентах остаётся требованием QA-03.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio

from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import (
    Attachment,
    IncomingMessage,
    MessageKind,
    Reply,
)
from fintracker.config import Settings
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.asr.provider import ScriptedAsrProvider, set_asr_override
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override


@dataclass
class BotUser:
    """Тестовый участник, общающийся с ботом в личном диалоге."""

    settings: Settings
    telegram_user_id: int
    chat_id: int
    clock: dt.datetime = field(
        default_factory=lambda: dt.datetime(2026, 9, 12, 9, 0, tzinfo=dt.UTC)
    )
    last_replies: list[Reply] = field(default_factory=list)

    async def send(
        self,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        at: dt.datetime | None = None,
    ) -> list[Reply]:
        message = IncomingMessage(
            telegram_user_id=self.telegram_user_id,
            chat_id=self.chat_id,
            kind=MessageKind.COMMAND if text.startswith("/") else MessageKind.TEXT,
            text=text,
            message_id=int(uuid.uuid4().int % 10**6),
            reply_to_message_id=reply_to_message_id,
            received_at=at or self.clock,
            correlation_id=uuid.uuid4().hex,
        )
        self.last_replies = await handle(self.settings, message)
        return self.last_replies

    async def press(self, data: str) -> list[Reply]:
        message = IncomingMessage(
            telegram_user_id=self.telegram_user_id,
            chat_id=self.chat_id,
            kind=MessageKind.CALLBACK,
            callback_data=data,
            received_at=self.clock,
            correlation_id=uuid.uuid4().hex,
        )
        self.last_replies = await handle(self.settings, message)
        return self.last_replies

    async def send_voice(self, *, duration_seconds: int = 5) -> list[Reply]:
        message = IncomingMessage(
            telegram_user_id=self.telegram_user_id,
            chat_id=self.chat_id,
            kind=MessageKind.VOICE,
            attachments=(
                Attachment(
                    file_id=f"voice-{uuid.uuid4().hex[:8]}",
                    kind="voice",
                    size_bytes=32_000,
                    mime_type="audio/ogg",
                    duration_seconds=duration_seconds,
                ),
            ),
            received_at=self.clock,
            correlation_id=uuid.uuid4().hex,
        )
        self.last_replies = await handle(self.settings, message)
        return self.last_replies

    async def send_photo(
        self,
        *,
        caption: str | None = None,
        size_bytes: int = 120_000,
        count: int = 1,
        media_group_id: str | None = None,
    ) -> list[Reply]:
        message = IncomingMessage(
            telegram_user_id=self.telegram_user_id,
            chat_id=self.chat_id,
            kind=MessageKind.PHOTO,
            text=caption,
            media_group_id=media_group_id,
            attachments=tuple(
                Attachment(
                    file_id=f"photo-{uuid.uuid4().hex[:8]}",
                    kind="photo",
                    size_bytes=size_bytes,
                    mime_type="image/jpeg",
                    width=1200,
                    height=1600,
                )
                for _ in range(count)
            ),
            received_at=self.clock,
            correlation_id=uuid.uuid4().hex,
        )
        self.last_replies = await handle(self.settings, message)
        return self.last_replies

    def text(self) -> str:
        return "\n".join(reply.text for reply in self.last_replies)

    def button_data(self, needle: str) -> str:
        for reply in self.last_replies:
            for row in reply.buttons:
                for button in row:
                    if needle.casefold() in button.text.casefold():
                        return button.data
        raise AssertionError(f"Кнопка «{needle}» не найдена в ответе:\n{self.text()}")

    def has_button(self, needle: str) -> bool:
        try:
            self.button_data(needle)
        except AssertionError:
            return False
        return True


@pytest.fixture
def recording_sender() -> Iterator[RecordingSender]:
    sender = RecordingSender()
    set_sender_override(sender)
    yield sender
    set_sender_override(None)


@pytest.fixture
def scripted_ai() -> Iterator[ScriptedAIProvider]:
    provider = ScriptedAIProvider()
    set_provider_override(provider)
    yield provider
    set_provider_override(None)


@pytest.fixture
def scripted_asr() -> Iterator[ScriptedAsrProvider]:
    provider = ScriptedAsrProvider()
    set_asr_override(provider)
    yield provider
    set_asr_override(None)


@pytest_asyncio.fixture
async def bot(clean_db: None, test_settings: Settings) -> AsyncIterator[None]:
    yield


def make_user(settings: Settings, telegram_user_id: int) -> BotUser:
    return BotUser(settings=settings, telegram_user_id=telegram_user_id, chat_id=telegram_user_id)
