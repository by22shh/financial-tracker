"""Клавиатуры и короткие непрозрачные callback-данные (раздел 19.4 ТЗ, LIM-10)."""

from __future__ import annotations

import uuid

from fintracker.application.conversation.types import Button

# Telegram ограничивает размер callback data; используются короткие коды.
MAX_CALLBACK_BYTES = 64


def short(value: uuid.UUID) -> str:
    """Короткая форма ID для кнопки; полный ID восстанавливается по префиксу."""
    return value.hex[:16]


# Кнопка с этим префиксом открывает ответ новым сообщением и не заменяет
# исходное: код приглашения, обзор или напоминание остаются в чате.
KEEP_SOURCE_PREFIX = "+"


def callback(action: str, *parts: str) -> str:
    data = ":".join((action, *parts))
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"Слишком длинные данные кнопки: {data}")
    return data


def keep(data: str) -> str:
    """Данные кнопки, ответ на которую не заменяет исходное сообщение."""
    return data if data.startswith(KEEP_SOURCE_PREFIX) else KEEP_SOURCE_PREFIX + data


def main_menu() -> tuple[tuple[Button, ...], ...]:
    return (
        (
            Button("➕ Добавить трату", callback("menu", "add")),
            Button("📒 Бюджет", callback("menu", "budget")),
        ),
        (
            Button("🗂 Категории", callback("menu", "categories")),
            Button("🧾 История", callback("menu", "history")),
        ),
        (
            Button("📊 Аналитика", callback("menu", "analytics")),
            Button("🎯 Цели", callback("menu", "goals")),
        ),
        (
            Button("🗓 Платежи", callback("menu", "payments")),
            Button("⋯ Ещё", callback("menu", "more")),
        ),
    )


def more_menu() -> tuple[tuple[Button, ...], ...]:
    return (
        (
            Button("📒 Мои бюджеты", callback("menu", "budgets")),
            Button("👥 Участники", callback("menu", "members")),
        ),
        (
            Button("✅ Проверить учёт", callback("menu", "check")),
            Button("📁 Импорт / экспорт", callback("menu", "io")),
        ),
        (
            Button("⚙️ Настройки", callback("menu", "settings")),
            Button("❔ Помощь", callback("menu", "help")),
        ),
        (Button("← Назад", callback("menu", "main")),),
    )


def back_to_menu() -> tuple[tuple[Button, ...], ...]:
    return ((Button("🏠 Меню", callback("menu", "main")),),)


def budget_selected_menu() -> tuple[tuple[Button, ...], ...]:
    """Действия после выбора бюджета без смешивания выбора и настроек."""
    return (
        (Button("📒 К моим бюджетам", callback("menu", "budgets")),),
        (
            Button("🏠 Главное меню", callback("menu", "main")),
            Button("⚙️ Настройки бюджета", callback("menu", "settings")),
        ),
    )


def start_menu(
    *, returning: bool, unfinished: bool = False, active: bool = False
) -> tuple[tuple[Button, ...], ...]:
    """Стартовое меню: создание, вход по коду и доступное продолжение (FR-05)."""
    rows: list[tuple[Button, ...]] = []
    if active:
        rows.append((Button("📒 Открыть бюджет", callback("menu", "main")),))
    if unfinished:
        rows.append((Button("▶️ Продолжить настройку", callback("wiz", "resume")),))
    if returning:
        rows.append((Button("📒 Мои бюджеты", callback("menu", "budgets")),))
    rows.append(
        (
            Button("➕ Создать бюджет", callback("wiz", "start")),
            Button("🔑 Войти по коду", callback("join", "start")),
        )
    )
    return tuple(rows)


def transaction_card(
    transaction_id: uuid.UUID, *, detailed: bool = False
) -> tuple[tuple[Button, ...], ...]:
    code = short(transaction_id)
    return (
        (
            Button("✏️ Изменить", callback("tx", "edit", code)),
            Button(
                "Свернуть" if detailed else "Подробнее",
                callback("tx", "open" if detailed else "details", code),
            ),
        ),
        (Button("↩️ Отменить запись", callback("tx", "void", code)),),
    )


def confirm_candidate(draft_id: uuid.UUID) -> tuple[tuple[Button, ...], ...]:
    code = short(draft_id)
    return (
        (Button("✅ Записать", callback("dr", "post", code)),),
        (
            Button("✏️ Изменить", callback("dr", "edit", code)),
            Button("✕ Отменить", callback("dr", "cancel", code)),
        ),
    )


__all__ = [
    "KEEP_SOURCE_PREFIX",
    "MAX_CALLBACK_BYTES",
    "Button",
    "back_to_menu",
    "callback",
    "confirm_candidate",
    "keep",
    "main_menu",
    "more_menu",
    "short",
    "start_menu",
    "transaction_card",
]
