"""Entry point for the sheet-only bot. Legacy budget jobs cannot be started."""

import argparse
import asyncio

from aiogram import Bot

from fintracker.sheetbot.bridge import SheetsBridge
from fintracker.sheetbot.config import BotSettings


async def check(settings: BotSettings) -> int:
    missing = settings.missing()
    if missing:
        print("Не настроено: " + ", ".join(missing))
        return 1
    bridge = SheetsBridge(settings.sheets)
    catalog = await bridge.latest_catalog()
    print(
        f"Последний лист {catalog.title}: "
        f"{len(catalog.categories)} категорий, {len(catalog.dates)} дней"
    )
    return 0


async def disable_webhook(settings: BotSettings) -> None:
    bot = Bot(settings.telegram.bot_token.get_secret_value())
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fintracker", description="Расходы в Google Sheets")
    parser.add_argument("command", choices=["poll", "check", "disable-webhook"])
    args = parser.parse_args(argv)
    settings = BotSettings()
    if args.command == "check":
        return asyncio.run(check(settings))
    if args.command == "disable-webhook":
        asyncio.run(disable_webhook(settings))
        return 0
    from fintracker.sheetbot.runtime import run

    asyncio.run(run(settings))
    return 0
