"""Часы приложения — единая точка времени для проверок управляемым временем."""

from __future__ import annotations

import datetime as dt
from typing import Protocol


class Clock(Protocol):
    def now(self) -> dt.datetime:  # UTC-aware
        ...


class SystemClock:
    __slots__ = ()

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


class FixedClock:
    """Управляемое время для проверок календаря и расписаний (QA-01)."""

    __slots__ = ("_now",)

    def __init__(self, now: dt.datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FixedClock требует времени с часовым поясом")
        self._now = now.astimezone(dt.UTC)

    def now(self) -> dt.datetime:
        return self._now

    def set(self, now: dt.datetime) -> None:
        self._now = now.astimezone(dt.UTC)

    def advance(self, delta: dt.timedelta) -> None:
        self._now += delta


def local_date(moment: dt.datetime, timezone: str) -> dt.date:
    """Локальная календарная дата события в поясе бюджета (FR-26)."""
    from zoneinfo import ZoneInfo

    return moment.astimezone(ZoneInfo(timezone)).date()
