"""Клавиатуры и короткие непрозрачные callback-данные (раздел 19.4 ТЗ, LIM-10)."""

from __future__ import annotations

import uuid

from fintracker.application.conversation.types import Button

# Telegram ограничивает размер callback data; используются короткие коды.
MAX_CALLBACK_BYTES = 64


def short(value: uuid.UUID) -> str:
    """Короткая форма ID для кнопки; полный ID восстанавливается по префиксу."""
    return value.hex[:16]


def callback(action: str, *parts: str) -> str:
    data = ":".join((action, *parts))
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"Слишком длинные данные кнопки: {data}")
    return data


def main_menu() -> tuple[tuple[Button, ...], ...]:
    return (
        (
            Button("Бюджет", callback("menu", "budget")),
            Button("Категории", callback("menu", "categories")),
        ),
        (
            Button("История", callback("menu", "history")),
            Button("Аналитика", callback("menu", "analytics")),
        ),
        (
            Button("Цели", callback("menu", "goals")),
            Button("Ещё", callback("menu", "more")),
        ),
    )


def more_menu() -> tuple[tuple[Button, ...], ...]:
    return (
        (
            Button("Мои бюджеты", callback("menu", "budgets")),
            Button("Участники", callback("menu", "members")),
        ),
        (
            Button("Платежи", callback("menu", "payments")),
            Button("Импорт/экспорт", callback("menu", "io")),
        ),
        (
            Button("Настройки", callback("menu", "settings")),
            Button("Помощь", callback("menu", "help")),
        ),
        (Button("← Назад", callback("menu", "main")),),
    )


def start_menu(*, returning: bool) -> tuple[tuple[Button, ...], ...]:
    rows: list[tuple[Button, ...]] = [
        (
            Button("Создать бюджет", callback("wiz", "start")),
            Button("Присоединиться по коду", callback("join", "start")),
        )
    ]
    if returning:
        rows.append((Button("Мои бюджеты", callback("menu", "budgets")),))
    return tuple(rows)


def transaction_card(transaction_id: uuid.UUID) -> tuple[tuple[Button, ...], ...]:
    code = short(transaction_id)
    return (
        (
            Button("Изменить", callback("tx", "edit", code)),
            Button("Категория", callback("tx", "cat", code)),
        ),
        (
            Button("Комментарий", callback("tx", "note", code)),
            Button("Отменить запись", callback("tx", "void", code)),
        ),
    )


def confirm_candidate(draft_id: uuid.UUID) -> tuple[tuple[Button, ...], ...]:
    code = short(draft_id)
    return (
        (
            Button("Записать", callback("dr", "post", code)),
            Button("Изменить", callback("dr", "edit", code)),
        ),
        (Button("Отменить", callback("dr", "cancel", code)),),
    )


__all__ = [
    "MAX_CALLBACK_BYTES",
    "Button",
    "callback",
    "confirm_candidate",
    "main_menu",
    "more_menu",
    "short",
    "start_menu",
    "transaction_card",
]
