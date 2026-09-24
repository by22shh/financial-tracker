"""Telegram-адаптер на aiogram 3 (ADR-05).

aiogram используется для типов, маршрутизации сохранённого Update и клиента
Telegram. Его обработчик с немедленным ответом и in-process фоновой
обработкой не заменяет долговечный inbox: webhook сохраняет событие и задачу
до ответа 2xx, а разбор выполняет worker.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from fintracker.config import Settings
from fintracker.core.logging import get_logger

logger = get_logger("bot.dispatcher")

# Команды-дубликаты пунктов меню (раздел 6.1 ТЗ).
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "👋 Начало работы"),
    ("add", "➕ Добавить трату по шагам"),
    ("budget", "💰 Остаток по плану"),
    ("history", "🧾 История и поиск"),
    ("categories", "🗂 Категории и лимиты"),
    ("report", "📊 Расходы и прогноз"),
    ("payments", "🗓 Регулярные платежи"),
    ("goals", "🎯 Цели накоплений"),
    ("members", "👥 Участники и приглашения"),
    ("budgets", "📒 Мои бюджеты"),
    ("join", "🔑 Войти в бюджет по коду"),
    ("settings", "⚙️ Настройки"),
    ("export", "📁 Импорт и экспорт"),
    ("cancel", "✕ Прервать текущий ввод"),
    ("help", "❔ Помощь"),
)


def build_bot(settings: Settings) -> Bot:
    token = settings.telegram.bot_token.get_secret_value()
    if not token:
        raise RuntimeError("Не задан токен Telegram (BL-03)")
    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))


def build_dispatcher() -> Dispatcher:
    """Диспетчер aiogram для типизированной маршрутизации сохранённых Update."""
    return Dispatcher()


async def configure_webhook(settings: Settings) -> dict[str, Any]:
    """Установить webhook с секретным заголовком (TECH-01)."""
    bot = build_bot(settings)
    base = settings.telegram.webhook_base_url.rstrip("/")
    if not base:
        raise RuntimeError("Не задан FINTRACKER_TELEGRAM__WEBHOOK_BASE_URL (BL-03)")
    secret = settings.telegram.webhook_secret.get_secret_value()
    if not secret:
        raise RuntimeError("Не задан секрет webhook, отдельный от токена бота")
    try:
        await bot.set_webhook(
            url=f"{base}/v1/telegram/webhook",
            secret_token=secret,
            drop_pending_updates=False,
            allowed_updates=["message", "edited_message", "callback_query", "my_chat_member"],
        )
        await bot.set_my_commands(
            [BotCommand(command=name, description=title) for name, title in BOT_COMMANDS]
        )
        info = await bot.get_webhook_info()
        return {
            "url": info.url,
            "pending_update_count": info.pending_update_count,
            "has_custom_certificate": info.has_custom_certificate,
        }
    finally:
        await bot.session.close()


async def register_commands(settings: Settings) -> None:
    bot = build_bot(settings)
    try:
        await bot.set_my_commands(
            [BotCommand(command=name, description=title) for name, title in BOT_COMMANDS]
        )
    finally:
        await bot.session.close()
