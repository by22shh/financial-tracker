"""Обзоры в диалоге: неделя, итог периода и план следующего (FR-55, FR-56, FR-61)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_weekly_review_available_by_command_and_button(
    bot: None, test_settings: Settings
) -> None:
    """FR-55: обзор недели открывается командой и кнопкой из отчёта."""
    user = make_user(test_settings, 906001)
    await create_budget(user, limits="Продукты 20000")
    await user.send("продукты 1500")

    await user.send("/review")
    text = user.text()
    assert "Обзор недели" in text
    assert "Потрачено" in text
    assert "Полнота учёта" in text
    assert text.count("💡 Что сделать") == 1

    await user.send("/report")
    assert user.has_button("Обзор недели")
    await user.press(user.button_data("Обзор недели"))
    assert "Обзор недели" in user.text()


async def test_period_summary_and_next_plan(bot: None, test_settings: Settings) -> None:
    """FR-56, FR-61: итог периода ведёт к проекту плана следующего."""
    user = make_user(test_settings, 906002)
    await create_budget(user, limits="Продукты 20000")
    await user.send("продукты 3000")

    await user.send("/summary")
    text = user.text()
    assert "Итоги периода" in text
    assert "Расходы:" in text
    # Остаток лимита не называется экономией без подтверждённой полноты.
    assert "сэкономил" not in text.lower()
    assert user.has_button("Следующий план")

    await user.press(user.button_data("Следующий план"))
    plan = user.text()
    assert "План на" in plan
    assert "Ожидаемый доход" in plan
    assert "Ожидаемый доход" in plan
    assert "🔁 " in plan


async def test_plan_command_keeps_income_and_balance_separate(
    bot: None, test_settings: Settings
) -> None:
    """FR-61: имеющийся остаток и ожидаемый доход показаны раздельно."""
    user = make_user(test_settings, 906003)
    await create_budget(user, limits="Продукты 20000")
    await user.send("/plan")
    text = user.text()
    income_line = next(line for line in text.splitlines() if "Ожидаемый доход" in line)
    # Без счетов с полным отслеживанием остаток не выдумывается.
    assert "На счетах" not in income_line
    assert "На счетах сейчас: 0" not in text
    assert "Лимиты по категориям" in text


async def test_help_lists_review_commands(bot: None, test_settings: Settings) -> None:
    """FR-55, FR-56: команды обзора видны в справке."""
    user = make_user(test_settings, 906004)
    await create_budget(user)
    await user.send("/help")
    text = user.text()
    assert "/review" in text
    assert "/summary" in text
    assert "/plan" in text
