"""Text or voice expenses go directly to the last visible worksheet."""

import json
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot

from fintracker.core.errors import DomainError
from fintracker.infra.ai.openai_client import AIProvider
from fintracker.infra.asr.provider import AsrProvider
from fintracker.sheetbot.bridge import BridgeError, SheetsBridge
from fintracker.sheetbot.config import BotSettings
from fintracker.sheetbot.extraction import extract
from fintracker.sheetbot.messages import HELP, WELCOME, clarification, formatted, notice, receipt
from fintracker.sheetbot.models import Catalog, Expense, Reply
from fintracker.sheetbot.store import Store


class SheetBot:
    def __init__(
        self,
        settings: BotSettings,
        store: Store,
        bridge: SheetsBridge,
        provider: AIProvider,
        asr: AsrProvider,
        bot: Bot,
    ) -> None:
        self.settings, self.store, self.bridge = settings, store, bridge
        self.provider, self.asr, self.bot = provider, asr, bot

    async def handle(self, update: dict[str, Any]) -> Reply | None:
        query = update.get("callback_query")
        message = (query or {}).get("message") or update.get("message") or {}
        sender = (query or message).get("from") or {}
        user_id = sender.get("id")
        if not user_id or (message.get("chat") or {}).get("type") != "private":
            return None
        if user_id not in self.settings.allowed_users:
            return notice(
                "🔒 Доступ не настроен",
                "Ваш Telegram-аккаунт пока не подключён к этой таблице.\n"
                "Попросите владельца бота добавить вас.",
            )
        event_id = update["update_id"]
        cached = self.store.field(event_id, "reply")
        if cached:
            return Reply.model_validate(cached)
        # Retry an already prepared write with the SAME key, sheet and amounts.
        prepared = self.store.field(event_id, "prepared")
        if prepared:
            return await self.commit(event_id, user_id, prepared)
        try:
            return await self.route(event_id, user_id, query, message)
        except BridgeError as exc:
            if exc.retryable:
                raise
            return notice("⚠️ Не записано", str(exc))
        except DomainError as exc:
            return notice("⚠️ Не записано", exc.message)

    async def route(
        self, event_id: int, user_id: int, query: dict[str, Any] | None, message: dict[str, Any]
    ) -> Reply:
        if query:
            return formatted(HELP)
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0] if text.startswith("/") else ""
        user = self.store.user(user_id)
        if command == "/start":
            return formatted(WELCOME)
        if command in {"/help", "/sheets"}:
            return formatted(HELP)
        if command == "/cancel":
            self.store.pending(user_id, None)
            return notice(
                "👌 Уточнение отменено", "Пришлите следующий расход — текстом или голосом."
            )
        if command:
            return formatted(HELP)
        voice = message.get("voice")
        if voice:
            if voice.get("duration", 0) > self.settings.asr.max_audio_seconds:
                return notice(
                    "🎙 Слишком длинное голосовое",
                    f"Пришлите запись до {self.settings.asr.max_audio_seconds} секунд.\n"
                    "Можно разделить рассказ на несколько сообщений.",
                )
            if voice.get("file_size", 0) > 15 * 1024 * 1024:
                return notice(
                    "🎙 Голосовое слишком большое",
                    "Пришлите файл до 15 МБ или напишите расход текстом.",
                )
            file = await self.bot.get_file(voice["file_id"])
            if not file.file_path or (file.file_size or 0) > 15 * 1024 * 1024:
                return notice(
                    "🎙 Не удалось загрузить голосовое",
                    "Пришлите запись до 15 МБ или напишите расход текстом.",
                )
            audio = await self.bot.download_file(file.file_path)
            if audio is None:
                return notice(
                    "🎙 Не удалось загрузить голосовое",
                    "Отправьте его ещё раз или напишите расход текстом.",
                )
            raw = audio.read(15 * 1024 * 1024 + 1)
            if len(raw) > 15 * 1024 * 1024:
                return notice(
                    "🎙 Голосовое слишком большое",
                    "Пришлите файл до 15 МБ или напишите расход текстом.",
                )
            transcript = await self.asr.transcribe(
                audio=raw,
                mime_type="audio/ogg",
                duration_seconds=voice["duration"],
            )
            if not transcript.speech_detected or not transcript.text.strip():
                return notice(
                    "🎙 Не удалось разобрать речь",
                    "Попробуйте записать голосовое ещё раз или напишите покупку и сумму.",
                )
            text = transcript.text.strip()
        if not text:
            return notice(
                "✍️ Пришлите расход",
                "Напишите покупку и сумму или отправьте голосовое.\nНапример: «Кофе 250 рублей».",
            )
        if len(text) > 4000:
            return notice(
                "✍️ Слишком много текста",
                "Разделите расходы на несколько сообщений — так я смогу разобрать каждую трату.",
            )
        catalog = await self.bridge.latest_catalog()
        # Relative dates are anchored to the Telegram message, not retry time.
        reference = (
            datetime.fromtimestamp(message["date"], UTC)
            .astimezone(ZoneInfo(self.settings.sheets.timezone))
            .date()
        )
        pending = json.loads(user["pending"]) if user["pending"] else None
        if pending and pending.get("sheet_id") != catalog.id:
            self.store.pending(user_id, None)
            return notice(
                "📊 Лист для записи изменился",
                f"Теперь расходы идут в «{catalog.title}».\n\n"
                "Пришлите расход целиком — прежнее уточнение отменено.",
            )
        previous = pending["text"] if pending else None
        if pending:
            reference = date.fromisoformat(pending["reference_date"])
        result = await extract(
            self.provider,
            text=text,
            previous_text=previous,
            catalog=catalog,
            reference_date=reference,
            currency=self.settings.sheets.currency,
        )
        if result.clarification:
            combined = f"{previous}\nУточнение: {text}" if previous else text
            # Save before returning; bounded context is sufficient for a short expense.
            self.store.pending(
                user_id,
                json.dumps(
                    {
                        "text": combined[-8000:],
                        "reference_date": reference.isoformat(),
                        "sheet_id": catalog.id,
                    },
                    ensure_ascii=False,
                ),
            )
            return clarification(result.clarification)
        prepared = {
            "key": f"telegram:{self.bot.id}:{message['chat']['id']}:{message['message_id']}",
            "catalog": catalog.model_dump(mode="json"),
            "expenses": [e.model_dump(mode="json") for e in result.expenses],
        }
        self.store.save(event_id, "prepared", prepared)
        return await self.commit(event_id, user_id, prepared)

    async def commit(self, event_id: int, user_id: int, prepared: dict[str, Any]) -> Reply:
        catalog = Catalog.model_validate(prepared["catalog"])
        expenses = [Expense.model_validate(e) for e in prepared["expenses"]]
        try:
            await self.bridge.write(key=prepared["key"], catalog=catalog, expenses=expenses)
        except BridgeError as exc:
            if exc.retryable:
                raise
            reply = notice("⚠️ Не записано", str(exc))
            self.store.save(event_id, "reply", reply.model_dump())
            return reply
        self.store.pending(user_id, None)
        reply = receipt(catalog, expenses, self.settings.sheets.currency)
        self.store.save(event_id, "reply", reply.model_dump())
        return reply


def chat_id_for(update: dict[str, Any]) -> int | None:
    query = update.get("callback_query") or {}
    message = query.get("message") or update.get("message") or {}
    return (message.get("chat") or {}).get("id")


def decode_event(event: dict[str, Any]) -> dict[str, Any]:
    return dict(json.loads(event["payload"]))
