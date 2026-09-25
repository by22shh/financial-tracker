"""The complete user flow: select sheet → say expense → automatic write."""

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
from fintracker.sheetbot.models import Catalog, Expense, Reply
from fintracker.sheetbot.store import Store

HELP = (
    "Отправьте расход текстом или голосом: «продукты 1250», «вчера доставка 890».\n"
    "Я выберу категорию на вашем листе и прибавлю сумму к нужному дню.\n\n"
    "/sheets — выбрать лист\n/cancel — отменить уточнение\n/help — помощь"
)


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

    async def choose_sheet(self) -> Reply:
        sheets = await self.bridge.sheets()
        if not sheets:
            return Reply(text="В таблице нет доступных листов расходов. Проверьте её структуру.")
        return Reply(
            text="Выберите лист для записи расходов:",
            buttons=[[{"text": s.title[:64], "callback_data": f"sheet:{s.id}"}] for s in sheets],
        )

    async def handle(self, update: dict[str, Any]) -> Reply | None:
        query = update.get("callback_query")
        message = (query or {}).get("message") or update.get("message") or {}
        sender = (query or message).get("from") or {}
        user_id = sender.get("id")
        if not user_id or (message.get("chat") or {}).get("type") != "private":
            return None
        if user_id not in self.settings.allowed_users:
            return Reply(text="Доступ к этой таблице не настроен для вашего Telegram-аккаунта.")
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
            return Reply(text=str(exc))
        except DomainError as exc:
            return Reply(text=f"Не записано. {exc.message}")

    async def route(
        self, event_id: int, user_id: int, query: dict[str, Any] | None, message: dict[str, Any]
    ) -> Reply:
        if query:
            data = query.get("data", "")
            if data.startswith("sheet:") and data[6:].isdigit():
                sheet_id = int(data[6:])
                sheets = await self.bridge.sheets()
                selected = next((s for s in sheets if s.id == sheet_id), None)
                if selected is None:
                    return await self.choose_sheet()
                await self.bridge.catalog(sheet_id)
                self.store.select(user_id, sheet_id)
                return Reply(text=f"Лист: {selected.title}\n\n{HELP}")
            return await self.choose_sheet()
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0] if text.startswith("/") else ""
        user = self.store.user(user_id)
        if command in {"/start", "/sheets"}:
            return await self.choose_sheet()
        if command == "/help":
            return Reply(text=HELP)
        if command == "/cancel":
            self.store.pending(user_id, None)
            return Reply(text="Уточнение отменено. Отправьте следующий расход.")
        if command:
            return Reply(text=HELP)
        if user["sheet_id"] is None:
            return await self.choose_sheet()
        voice = message.get("voice")
        if voice:
            if voice.get("duration", 0) > self.settings.asr.max_audio_seconds:
                return Reply(
                    text=f"Пришлите голосовое до {self.settings.asr.max_audio_seconds} секунд."
                )
            if voice.get("file_size", 0) > 15 * 1024 * 1024:
                return Reply(text="Голосовое слишком большое. Пришлите файл до 15 МБ.")
            file = await self.bot.get_file(voice["file_id"])
            if not file.file_path or (file.file_size or 0) > 15 * 1024 * 1024:
                return Reply(text="Не удалось загрузить голосовое до 15 МБ.")
            audio = await self.bot.download_file(file.file_path)
            if audio is None:
                return Reply(text="Не удалось загрузить голосовое. Пришлите его ещё раз.")
            raw = audio.read(15 * 1024 * 1024 + 1)
            if len(raw) > 15 * 1024 * 1024:
                return Reply(text="Голосовое слишком большое. Пришлите файл до 15 МБ.")
            transcript = await self.asr.transcribe(
                audio=raw,
                mime_type="audio/ogg",
                duration_seconds=voice["duration"],
            )
            if not transcript.speech_detected or not transcript.text.strip():
                return Reply(
                    text="Не удалось разобрать речь. Напишите расход или повторите запись."
                )
            text = transcript.text.strip()
        if not text:
            return Reply(text="Пришлите расход текстом или голосовым сообщением.")
        if len(text) > 4000:
            return Reply(
                text="Сообщение слишком длинное. Разделите расходы на несколько сообщений."
            )
        catalog = await self.bridge.catalog(user["sheet_id"])
        # Relative dates are anchored to the Telegram message, not retry time.
        reference = (
            datetime.fromtimestamp(message["date"], UTC)
            .astimezone(ZoneInfo(self.settings.sheets.timezone))
            .date()
        )
        pending = json.loads(user["pending"]) if user["pending"] else None
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
                    {"text": combined[-8000:], "reference_date": reference.isoformat()},
                    ensure_ascii=False,
                ),
            )
            return Reply(text=result.clarification + "\n\n/cancel — отменить ввод")
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
            reply = Reply(text=f"Не записано. {exc}")
            self.store.save(event_id, "reply", reply.model_dump())
            return reply
        self.store.pending(user_id, None)
        labels = {c.id: c.label for c in catalog.categories}
        lines = [f"Записано в «{catalog.title}»:"]
        for e in expenses:
            amount = f"{e.amount_minor // 100:,}".replace(",", " ")
            if e.amount_minor % 100:
                amount += f",{e.amount_minor % 100:02d}"
            lines.append(
                f"{labels[e.category_id]} · {amount} {self.settings.sheets.currency} "
                f"· {e.date:%d.%m.%Y}"
            )
        reply = Reply(text="\n".join(lines))
        self.store.save(event_id, "reply", reply.model_dump())
        return reply


def chat_id_for(update: dict[str, Any]) -> int | None:
    query = update.get("callback_query") or {}
    message = query.get("message") or update.get("message") or {}
    return (message.get("chat") or {}).get("id")


def decode_event(event: dict[str, Any]) -> dict[str, Any]:
    return dict(json.loads(event["payload"]))
