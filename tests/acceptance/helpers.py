"""Помощники сквозных сценариев: прохождение мастера создания бюджета."""

from __future__ import annotations

from tests.acceptance.conftest import BotUser

DEFAULT_CATEGORIES = "Продукты, Рестораны, Транспорт, Жильё"


async def create_budget(
    user: BotUser,
    *,
    name: str = "Наш общий бюджет",
    currency: str = "RUB",
    timezone: str = "Asia/Novosibirsk",
    period: str = "10.09.2026 — 09.10.2026",
    income: str = "100000",
    categories: str = DEFAULT_CATEGORIES,
    limits: str | None = None,
    repeat_template: bool = True,
    accept_deficit: bool = False,
) -> str:
    """Пройти мастер до создания бюджета и вернуть текст итогового ответа."""
    await user.send("/start")
    await user.press("wiz:start")
    await user.send(name)
    await user.send(currency)
    await user.send(timezone)
    await user.send(period)
    # Первый вариант повторения — календарный месяц при согласованных датах.
    await user.press(user.button_data("календарный месяц"))
    await user.press("wiz:inc:exact")
    await user.send(income)
    await user.send(categories)
    if limits:
        await user.send(limits)
    else:
        await user.press("wiz:skip:limits")
    await user.press("wiz:skip:commitments")
    await user.press("wiz:skip:goals")
    await user.press("wiz:tpl:on" if repeat_template else "wiz:tpl:off")
    if accept_deficit and user.has_button("дефицит"):
        await user.press(user.button_data("дефицит"))
    await user.press("wiz:publish")
    return user.text()


async def issue_invite_code(user: BotUser) -> str:
    """Выпустить код приглашения и вернуть его в нормализованном виде."""
    await user.press("inv:new")
    text = user.text()
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) == 14 and stripped.count("-") == 2:
            return stripped
    raise AssertionError(f"Код приглашения не найден в ответе:\n{text}")
