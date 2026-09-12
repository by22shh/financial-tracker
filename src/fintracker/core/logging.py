"""Структурированные логи без финансового payload (ADR-13, SEC-06)."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from fintracker.config import ObservabilitySettings

# Значения, которые никогда не попадают в логи (SEC-06).
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bot_token",
        "invite_code",
        "note",
        "password",
        "raw_text",
        "secret",
        "token",
        "transcript",
        "webhook_secret",
    }
)
_TELEGRAM_FILE_URL = re.compile(r"https://api\.telegram\.org/file/bot[^\s\"']+")
_BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def _redact(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = "<redacted>"
        elif isinstance(event_dict[key], str):
            value = _TELEGRAM_FILE_URL.sub("<telegram-file-url>", event_dict[key])
            event_dict[key] = _BOT_TOKEN.sub("<bot-token>", value)
    return event_dict


def configure_logging(settings: ObservabilitySettings) -> None:
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
