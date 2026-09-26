"""Text or voice expenses go directly to the last visible worksheet."""

import json
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot

from fintracker.core.errors import DomainError
from fintracker.infra.ai.openai_client import AIProvider
from fintracker.infra.asr.provider import AsrProvider
from fintracker.sheetbot.bridge import BridgeError, SheetsBridge
from fintracker.sheetbot.config import BotSettings
from fintracker.sheetbot.extraction import extract
from fintracker.sheetbot.menu import ACTIONS, REPORT_SCOPES
from fintracker.sheetbot.messages import (
    HELP,
    WELCOME,
    category_overview_message,
    clarification,
    formatted,
    notice,
    receipt,
    summary_message,
    voice_preview,
    with_buttons,
)
from fintracker.sheetbot.models import Catalog, CategoryStatus, Expense, Reply, ReportRequest
from fintracker.sheetbot.store import Store

EDIT_WINDOW_SECONDS = 15 * 60


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
            reply = await self.route(event_id, user_id, query, message)
            self.store.save(event_id, "reply", reply.model_dump())
            return reply
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
            return await self.callback(event_id, user_id, query)
        text = (message.get("text") or "").strip()
        command = ACTIONS.get(text, "")
        if text.casefold() == "меню":
            return notice(
                "🧾 Главное меню",
                "Выберите сводку на кнопках внизу. Чтобы записать расход, "
                "просто отправьте текст или голосовое.",
            )
        if not command and text.startswith("/"):
            command = text.split()[0].split("@")[0]
        user = self.store.user(user_id)
        if command == "/start":
            return formatted(WELCOME)
        if command in {"/help", "/sheets"}:
            return formatted(HELP)
        if command == "/cancel":
            self.store.pending(user_id, None)
            return notice("👌 Ввод отменён", "Пришлите следующий расход — текстом или голосом.")
        if command == "/categories":
            return await self.current_category_overview(page=0)
        if command in {"/today", "/week", "/summary", "/period"}:
            catalog = await self.bridge.latest_catalog()
            reference = self.reference(message)
            if command == "/period":
                return await self.offer_period(event_id, user_id, catalog, reference)
            return await self.report(
                catalog,
                reference,
                ReportRequest(scope=REPORT_SCOPES[command]),
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
        reference = self.reference(message)
        pending = json.loads(user["pending"]) if user["pending"] else None
        if pending and pending.get("kind") == "edit":
            started_at = pending.get("started_at")
            age = (
                datetime.now(UTC).timestamp() - started_at
                if isinstance(started_at, int | float)
                else -1
            )
            if not 0 <= age <= EDIT_WINDOW_SECONDS:
                self.store.pending(user_id, None)
                pending = None
        if pending and pending.get("sheet_id") != catalog.id:
            self.store.pending(user_id, None)
            return notice(
                "📊 Лист для записи изменился",
                f"Теперь расходы идут в «{catalog.title}».\n\n"
                "Пришлите расход целиком — прежнее уточнение отменено.",
            )
        if pending and pending.get("kind") == "edit":
            return await self.edit(event_id, user_id, message, text, bool(voice), catalog, pending)
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
            preferences=self.store.preferences(user_id),
        )
        if result.report:
            return voice_preview(
                await self.report(catalog, reference, result.report), text if voice else None
            )
        if catalog.dates and reference > max(catalog.dates):
            return voice_preview(
                await self.offer_period(event_id, user_id, catalog, reference),
                text if voice else None,
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
            return voice_preview(clarification(result.clarification), text if voice else None)
        prepared = {
            "record_id": event_id,
            "timestamp": message["date"],
            "voice": text if voice else None,
            "key": f"telegram:{self.bot.id}:{message['chat']['id']}:{message['message_id']}",
            "catalog": catalog.model_dump(mode="json"),
            "expenses": [e.model_dump(mode="json") for e in result.expenses],
        }
        signature = self.signature(prepared)
        if any(
            self.signature(old) == signature for old in self.store.recent(user_id, message["date"])
        ):
            self.store.pending(user_id, None)
            self.store.save_prompt(event_id, user_id, "duplicate", prepared)
            return voice_preview(
                with_buttons(
                    notice(
                        "🔎 Похожая трата уже записана",
                        "За последние 5 минут вы уже добавляли такую сумму, категорию и дату. "
                        "Записать ещё одну?",
                    ),
                    [
                        [("Да, это ещё одна трата", f"confirm:{event_id}")],
                        [("Не добавлять", f"dismiss:{event_id}")],
                    ],
                ),
                text if voice else None,
            )
        return await self.prepare(event_id, user_id, prepared)

    @staticmethod
    def signature(record: dict[str, Any]) -> tuple[Any, ...]:
        expenses = record["expenses"]
        return (
            record["catalog"]["id"],
            tuple(sorted((e["amount_minor"], e["category_id"], e["date"]) for e in expenses)),
        )

    def reference(self, message: dict[str, Any]) -> date:
        return (
            datetime.fromtimestamp(message["date"], UTC)
            .astimezone(ZoneInfo(self.settings.sheets.timezone))
            .date()
        )

    def key(self, user_id: int, event_id: int) -> str:
        return f"telegram:{self.bot.id}:{user_id}:op:{event_id}"

    async def prepare(self, event_id: int, user_id: int, prepared: dict[str, Any]) -> Reply:
        self.store.save(event_id, "prepared", prepared)
        return await self.commit(event_id, user_id, prepared)

    async def report(self, catalog: Catalog, reference: date, request: ReportRequest) -> Reply:
        target = reference - timedelta(days=request.scope == "yesterday")
        if request.scope in {"today", "yesterday"} and target not in catalog.dates:
            return notice(
                "📊 Эта дата вне текущего периода",
                f"На последнем листе «{catalog.title}» нет даты {target:%d.%m.%Y}. "
                "Нажмите «За весь период» или «Новый период» в меню.",
            )
        if request.scope == "week":
            start = reference - timedelta(days=6)
            dates = sorted(d for d in catalog.dates if start <= d <= reference)
            if not dates:
                return notice(
                    "📊 Неделя вне рабочего периода",
                    "На последнем листе нет дат за последние 7 дней. "
                    "Нажмите «За весь период» или «Новый период» в меню.",
                )
        else:
            dates = catalog.dates if request.scope == "period" else [target]
        data = await self.bridge.summary(
            sheet_id=catalog.id,
            revision=catalog.revision,
            dates=[d.isoformat() for d in dates],
            category_ids=request.category_ids,
        )
        return summary_message(data, self.settings.sheets.currency, scope=request.scope)

    async def current_category_overview(self, *, page: int) -> Reply:
        try:
            catalog = await self.bridge.latest_catalog()
            statuses = await self.bridge.category_status(
                sheet_id=catalog.id,
                revision=catalog.revision,
                category_ids=[category.id for category in catalog.categories],
            )
        except BridgeError as exc:
            if exc.retryable:
                raise
            return notice("⚠️ Не удалось показать категории", str(exc))
        return category_overview_message(
            catalog, statuses, self.settings.sheets.currency, page=page
        )

    async def offer_period(
        self, event_id: int, user_id: int, catalog: Catalog, reference: date
    ) -> Reply:
        if reference <= max(catalog.dates):
            return notice(
                "📅 Текущий период ещё идёт",
                f"Записываю расходы в «{catalog.title}». Новый лист пока не нужен.",
            )
        plan = await self.bridge.period(sheet_id=catalog.id, reference_date=reference.isoformat())
        self.store.save_prompt(
            event_id,
            user_id,
            "period",
            {
                "sheet_id": catalog.id,
                "revision": catalog.revision,
                "reference_date": reference.isoformat(),
                "start": plan["start"],
                "end": plan["end"],
            },
        )
        return with_buttons(
            notice(
                "📅 Начался новый период",
                f"Последний лист: «{catalog.title}».\nСоздать «{plan['title']}» по его шаблону?\n\n"
                "Сохраню категории, оформление и итоговые формулы. "
                "В новом листе расходы будут пустыми.",
            ),
            [[("📅 Создать период", f"confirm:{event_id}")], [("Позже", f"dismiss:{event_id}")]],
        )

    def record_reply(
        self, record_id: int, record: dict[str, Any], *, edited: bool = False
    ) -> Reply:
        expenses = [Expense.model_validate(e) for e in record["expenses"]]
        if not expenses:
            return notice("↩️ Расход отменён", "Сумма убрана из таблицы.")
        reply = receipt(
            Catalog.model_validate(record["catalog"]),
            expenses,
            self.settings.sheets.currency,
            [CategoryStatus.model_validate(item) for item in record.get("category_status", [])],
        )
        if edited:
            reply.text = reply.text.replace("✅ <b>Записано</b>", "✅ <b>Исправлено</b>", 1)
        version = record.get("version", 0)
        return voice_preview(
            with_buttons(
                reply,
                [
                    [
                        ("✏️ Изменить", f"edit:{record_id}:{version}"),
                        ("↩️ Отменить", f"undo:{record_id}:{version}"),
                    ]
                ],
            ),
            record.get("voice"),
        )

    async def callback(self, event_id: int, user_id: int, query: dict[str, Any]) -> Reply:
        parts = str(query.get("data", "")).split(":")
        action = parts[0]
        if action == "categories" and len(parts) == 2 and parts[1].isdigit():
            return await self.current_category_overview(page=int(parts[1]))
        expired = notice(
            "⌛ Эта кнопка больше не актуальна",
            "Используйте кнопки под последней квитанцией этой траты.",
        )
        if action in {"confirm", "dismiss"} and len(parts) == 2 and parts[1].isdigit():
            prompt_id = int(parts[1])
            prompt = self.store.prompt(prompt_id, user_id)
            if not prompt:
                return expired
            if action == "dismiss":
                self.store.close_prompt(prompt_id)
                self.store.pending(user_id, None)
                return notice("👌 Хорошо", "Ничего не добавлено. Пришлите следующую трату.")
            prepared = prompt["data"]
            if prompt["kind"] == "period":
                prepared = {
                    "action": "create_period",
                    "payload": prepared,
                    "key": self.key(user_id, event_id),
                }
            self.store.save(event_id, "prepared", prepared)
            self.store.close_prompt(prompt_id)
            return await self.commit(event_id, user_id, prepared)
        if action not in {"edit", "undo"} or len(parts) not in {3, 4}:
            return formatted(HELP)
        if not all(p.isdigit() for p in parts[1:]):
            return expired
        record_id, version = int(parts[1]), int(parts[2])
        record = self.store.record(record_id, user_id)
        if not record or record.get("version", 0) != version or not record["expenses"]:
            return expired
        catalog = await self.bridge.latest_catalog()
        if (
            catalog.id != record["catalog"]["id"]
            or catalog.revision != record["catalog"]["revision"]
        ):
            return notice("📊 Лист изменился", "Эту старую запись можно исправить в самой таблице.")
        if len(parts) == 3 and len(record["expenses"]) > 1:
            labels = {c.id: c.label for c in catalog.categories}
            return with_buttons(
                notice("🧾 Выберите трату", "Изменится только выбранный расход."),
                [
                    [
                        (
                            f"{i + 1}. {labels[e['category_id']][:35]} · "
                            f"{e['amount_minor'] / 100:g}",
                            f"{action}:{record_id}:{version}:{i}",
                        )
                    ]
                    for i, e in enumerate(record["expenses"])
                ],
            )
        index = int(parts[3]) if len(parts) == 4 else 0
        if index >= len(record["expenses"]):
            return expired
        if action == "undo":
            expenses = [e for i, e in enumerate(record["expenses"]) if i != index]
            return await self.prepare(
                event_id,
                user_id,
                {
                    **record,
                    "action": "amend",
                    "record_id": record_id,
                    "operation_key": self.key(user_id, event_id),
                    "expenses": expenses,
                    "old_expenses": record["expenses"],
                    "undo": True,
                },
            )
        self.store.pending(
            user_id,
            json.dumps(
                {
                    "kind": "edit",
                    "sheet_id": catalog.id,
                    "record_id": record_id,
                    "version": version,
                    "index": index,
                    "started_at": datetime.now(UTC).timestamp(),
                }
            ),
        )
        selected = record["expenses"][index]
        shown = receipt(catalog, [Expense.model_validate(selected)], self.settings.sheets.currency)
        shown.text = shown.text.replace("✅ <b>Записано</b>", "✏️ <b>Что исправить?</b>", 1)
        shown.text += (
            "\n\nНапишите или скажите: «Сумма 350», «Это кафе» или «Дата — вчера»."
            "\nОстальное сохраню. Режим исправления действует 15 минут."
            "\n«✖️ Отменить ввод» в меню — выйти без изменений."
        )
        return shown

    async def edit(
        self,
        event_id: int,
        user_id: int,
        message: dict[str, Any],
        text: str,
        voice: bool,
        catalog: Catalog,
        pending: dict[str, Any],
    ) -> Reply:
        record_id = pending["record_id"]
        record = self.store.record(record_id, user_id)
        if not record or record.get("version", 0) != pending["version"]:
            self.store.pending(user_id, None)
            return notice("⌛ Запись уже изменилась", "Нажмите «Изменить» под новой квитанцией.")
        if catalog.revision != record["catalog"]["revision"]:
            self.store.pending(user_id, None)
            return notice("📊 Структура листа изменилась", "Исправьте эту запись в самой таблице.")
        index = pending["index"]
        previous = json.dumps(record["expenses"][index], ensure_ascii=False)
        correction = pending.get("correction", "") + "\n" + text
        result = await extract(
            self.provider,
            text=correction,
            previous_text=previous,
            catalog=catalog,
            reference_date=self.reference(message),
            currency=self.settings.sheets.currency,
            preferences=self.store.preferences(user_id),
            editing=True,
        )
        if result.clarification:
            pending["correction"] = correction[-4000:]
            self.store.pending(user_id, json.dumps(pending))
            return voice_preview(clarification(result.clarification), text if voice else None)
        expenses = list(record["expenses"])
        expenses[index] = result.expenses[0].model_dump(mode="json")
        return await self.prepare(
            event_id,
            user_id,
            {
                **record,
                "action": "amend",
                "record_id": record_id,
                "operation_key": self.key(user_id, event_id),
                "expenses": expenses,
                "old_expenses": record["expenses"],
                "voice": text if voice else None,
                "learn_index": index,
            },
        )

    async def commit(self, event_id: int, user_id: int, prepared: dict[str, Any]) -> Reply:
        action = prepared.get("action", "write")
        try:
            if action == "create_period":
                result = await self.bridge.create_period(key=prepared["key"], **prepared["payload"])
                self.store.pending(user_id, None)
                reply = notice(
                    "📅 Новый период готов",
                    f"Создан лист «{result['title']}». Следующие расходы пойдут в него.\n\n"
                    "Пришлите расход ещё раз — пока я создал только лист.",
                )
            else:
                catalog = Catalog.model_validate(prepared["catalog"])
                expenses = [Expense.model_validate(e) for e in prepared["expenses"]]
                if action == "amend":
                    await self.bridge.amend(
                        key=prepared["operation_key"],
                        target_key=prepared["key"],
                        sheet_id=catalog.id,
                        revision=catalog.revision,
                        version=prepared.get("version", 0),
                        expenses=prepared["expenses"],
                    )
                else:
                    await self.bridge.write(key=prepared["key"], catalog=catalog, expenses=expenses)
                category_status: list[CategoryStatus] = []
                if expenses:
                    # Supplementary totals must not turn a committed expense
                    # into a misleading "not saved" reply.
                    with suppress(BridgeError):
                        category_status = await self.bridge.category_status(
                            sheet_id=catalog.id,
                            revision=catalog.revision,
                            category_ids=list(dict.fromkeys(e.category_id for e in expenses)),
                        )
                record_id = prepared.get("record_id", event_id)
                record = {k: prepared[k] for k in ("key", "catalog", "expenses")}
                record["category_status"] = [item.model_dump() for item in category_status]
                record["version"] = prepared.get("version", 0) + (action == "amend")
                record["voice"] = prepared.get("voice")
                self.store.save_record(record_id, user_id, record, prepared.get("timestamp", 0))
                if "learn_index" in prepared:
                    index = prepared["learn_index"]
                    old, new = prepared["old_expenses"][index], prepared["expenses"][index]
                    if old["category_id"] != new["category_id"]:
                        self.store.learn(user_id, old["description"], new["category_id"])
                self.store.pending(user_id, None)
                reply = self.record_reply(record_id, record, edited=action == "amend")
                if prepared.get("undo") and record["expenses"]:
                    reply.text = "↩️ <b>Трата отменена</b>\n\nОстальные расходы:\n\n" + reply.text
        except BridgeError as exc:
            if exc.retryable:
                raise
            reply = notice("⚠️ Изменения не сохранены", str(exc))
        self.store.save(event_id, "reply", reply.model_dump())
        return reply


def chat_id_for(update: dict[str, Any]) -> int | None:
    query = update.get("callback_query") or {}
    message = query.get("message") or update.get("message") or {}
    return (message.get("chat") or {}).get("id")


def decode_event(event: dict[str, Any]) -> dict[str, Any]:
    return dict(json.loads(event["payload"]))
