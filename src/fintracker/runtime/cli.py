"""Единый артефакт с командами api / worker / scheduler (ADR-01)."""

from __future__ import annotations

import argparse
import asyncio
import sys

from fintracker.config import get_settings
from fintracker.core.logging import configure_logging, get_logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fintracker", description="Финансовый трекер")
    subparsers = parser.add_subparsers(dest="command", required=True)
    api = subparsers.add_parser("api", help="HTTP API и приём webhook")
    api.add_argument("--host", default="0.0.0.0")  # noqa: S104
    api.add_argument("--port", type=int, default=8080)
    subparsers.add_parser("worker", help="Фоновый исполнитель задач")
    subparsers.add_parser("scheduler", help="Календарь и сроки")
    subparsers.add_parser("check", help="Проверить конфигурацию и готовность")
    subparsers.add_parser("poll", help="Long-polling режим бота для локальной проверки")
    subparsers.add_parser("set-webhook", help="Установить webhook и команды бота")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.observability)
    logger = get_logger("runtime")

    match args.command:
        case "api":
            import uvicorn

            from fintracker.api.app import create_app

            uvicorn.run(create_app(), host=args.host, port=args.port, log_config=None)
            return 0
        case "worker":
            from fintracker.runtime.worker import run_worker

            asyncio.run(run_worker(settings))
            return 0
        case "scheduler":
            from fintracker.runtime.scheduler import run_scheduler

            asyncio.run(run_scheduler(settings))
            return 0
        case "poll":
            from fintracker.runtime.polling import run_polling

            asyncio.run(run_polling(settings))
            return 0
        case "set-webhook":
            from fintracker.bot.dispatcher import configure_webhook

            info = asyncio.run(configure_webhook(settings))
            logger.info("webhook_configured", **info)
            return 0
        case "check":
            from fintracker.runtime.health import check_readiness

            report = asyncio.run(check_readiness(settings))
            logger.info("readiness", **report.to_payload())
            print(report.to_payload())
            return 0 if report.ready else 1
        case _:  # pragma: no cover
            parser.error(f"Неизвестная команда {args.command}")
            return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
