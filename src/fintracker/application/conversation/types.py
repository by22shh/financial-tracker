"""Типы разговорного слоя.

Слой не зависит от aiogram: Telegram-адаптер преобразует Update в
``IncomingMessage`` и отправляет полученные ``Reply``. Это позволяет
проверять полный пользовательский путь без реального Telegram, сохраняя
требование отдельной проверки на живых клиентах (QA-03).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class MessageKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    PHOTO = "photo"
    DOCUMENT = "document"
    CALLBACK = "callback"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    data: str

    def as_dict(self) -> dict[str, str]:
        return {"text": self.text, "callback_data": self.data}


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()
    # Карточка конкретной операции: ответ на неё адресует именно её (G-06).
    transaction_id: uuid.UUID | None = None
    # Ответ автору на только что введённую операцию показывается сразу (FR-53).
    immediate: bool = True
    # Управление диалогом не должно зависеть от формулировки или эмодзи ответа.
    retry_input: bool = False
    # Callback-навигация по возможности обновляет исходную карточку, чтобы
    # диалог не превращался в ленту одинаковых меню. Транспорт обязан
    # безопасно перейти к sendMessage, если редактирование недоступно.
    edit_message_id: int | None = None

    def keyboard(self) -> list[list[dict[str, str]]] | None:
        if not self.buttons:
            return None
        return [[button.as_dict() for button in row] for row in self.buttons]


@dataclass(frozen=True, slots=True)
class Attachment:
    file_id: str
    kind: str
    size_bytes: int | None = None
    mime_type: str | None = None
    file_name: str | None = None
    duration_seconds: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """Нормализованное входящее сообщение с закреплённым контекстом."""

    telegram_user_id: int
    chat_id: int
    kind: MessageKind
    text: str | None = None
    callback_data: str | None = None
    message_id: int | None = None
    reply_to_message_id: int | None = None
    media_group_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    received_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    # Контекст бюджета закрепляется при приёме события (FR-79).
    workspace_id: uuid.UUID | None = None
    inbound_event_id: uuid.UUID | None = None
    # Проверочное значение кода приглашения: открытый код не переносится (SEC-04).
    invite_digest: str | None = None
    correlation_id: str = ""
    # Имя из профиля Telegram: подпись участника в общем бюджете (FR-04).
    display_name: str | None = None

    @property
    def source_key(self) -> str | None:
        """Постоянный ключ пользовательского сообщения (R-01, R-03).

        Ключ одинаков для повторной обработки того же входа и для всех
        редакций одного сообщения Telegram, поэтому одно действие участника
        даёт один результат независимо от числа доставленных Update.
        """
        if self.kind is MessageKind.CALLBACK:
            return None
        if self.message_id is not None:
            return f"tg:{self.chat_id}:{self.message_id}"
        if self.inbound_event_id is not None:
            # Без номера сообщения идентичность даёт сохранённое событие: его
            # повторная обработка тоже не должна создавать вторую запись.
            return f"ev:{self.inbound_event_id}"
        return None

    @property
    def command(self) -> str | None:
        if self.text and self.text.startswith("/"):
            return self.text.split()[0].split("@")[0].lower()
        return None

    @property
    def command_argument(self) -> str:
        if self.text and self.text.startswith("/"):
            parts = self.text.split(maxsplit=1)
            return parts[1].strip() if len(parts) > 1 else ""
        return ""
