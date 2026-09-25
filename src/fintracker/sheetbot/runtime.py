"""Single-process polling with a durable inbox and ordered expense writes."""

import asyncio
import contextlib
import fcntl
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from fintracker.infra.ai.openai_client import build_provider
from fintracker.infra.asr.provider import build_asr
from fintracker.sheetbot.bridge import SheetsBridge
from fintracker.sheetbot.config import BotSettings
from fintracker.sheetbot.service import SheetBot, chat_id_for, decode_event
from fintracker.sheetbot.store import Store

COMMANDS = (
    ("start", "Выбрать лист"),
    ("sheets", "Сменить лист"),
    ("cancel", "Отменить уточнение"),
    ("help", "Как записать расход"),
)
logger = logging.getLogger(__name__)


async def run(settings: BotSettings) -> None:
    missing = settings.missing()
    if missing:
        raise RuntimeError("Не настроено: " + ", ".join(missing))
    settings.sheets.state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = settings.sheets.state_path.with_suffix(".lock")
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Уже запущен обработчик этой таблицы.") from exc
        store = Store(settings.sheets.state_path)
        bot = Bot(settings.telegram.bot_token.get_secret_value())
        try:
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise RuntimeError(
                    "У бота установлен webhook. Остановите старый бот и выполните "
                    "fintracker disable-webhook перед запуском."
                )
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
            service = SheetBot(
                settings,
                store,
                SheetsBridge(settings.sheets),
                build_provider(settings.ai),
                build_asr(settings.asr),
                bot,
            )
            async with asyncio.TaskGroup() as group:
                group.create_task(receive(bot, store))
                group.create_task(consume(bot, store, service))
        finally:
            await bot.session.close()
            store.db.close()


async def receive(bot: Bot, store: Store) -> None:
    while True:
        try:
            updates = await bot.get_updates(
                offset=store.offset(), timeout=25, allowed_updates=["message", "callback_query"]
            )
            for update in updates:
                # Handlers read Telegram wire names ("from"), not aiogram's "from_user".
                store.enqueue(update.model_dump(mode="json", exclude_none=True, by_alias=True))
                if update.callback_query:
                    with contextlib.suppress(Exception):
                        await bot.answer_callback_query(update.callback_query.id)
        except Exception as exc:
            logger.warning("telegram_receive_retry: %s", type(exc).__name__)
            await asyncio.sleep(3)


async def consume(bot: Bot, store: Store, service: SheetBot) -> None:
    delay = 2
    while True:
        event = store.next_event()
        if event is None:
            await asyncio.sleep(0.3)
            continue
        try:
            update = decode_event(event)
            reply = await service.handle(update)
            chat_id = chat_id_for(update)
            if reply and chat_id:
                store.save(event["id"], "reply", reply.model_dump())
                markup = (
                    InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=b["text"], callback_data=b["callback_data"]
                                )
                                for b in row
                            ]
                            for row in reply.buttons
                        ]
                    )
                    if reply.buttons
                    else None
                )
                await bot.send_message(chat_id, reply.text, reply_markup=markup)
            store.finish(event["id"])
            delay = 2
        except TelegramForbiddenError:
            store.finish(event["id"])
        except Exception as exc:
            # Keep the prepared write on disk. Retrying cannot add the sum twice.
            logger.warning("sheetbot_retry update=%s error=%s", event["id"], type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
