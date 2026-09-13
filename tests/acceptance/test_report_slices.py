"""Разрезы отчёта и уточнение периода (A53, A85, A86, A90, A91, A229)."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import BotUser, make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def _post(user: BotUser, text: str) -> None:
    await user.send(text)
    if user.has_button("Записать"):
        await user.press(user.button_data("Записать"))


async def test_a53_calendar_month_differs_from_budget_period(
    bot: None, test_settings: Settings
) -> None:
    """A53: «за календарный сентябрь» — 1–30 сентября, не бюджетные 10–9."""
    user = make_user(test_settings, 910001)
    await create_budget(user)
    await _post(user, "продукты 500")

    await user.send("Сколько потрачено за календарный сентябрь?")
    text = user.text()
    assert "Разрез: календарный месяц" in text
    assert "01.09.2026" in text or "1 сентября" in text


async def test_a229_month_question_on_short_period_is_clarified(
    bot: None, test_settings: Settings
) -> None:
    """A229: при недельном цикле «за месяц» уточняется, неделя не зовётся месяцем."""
    user = make_user(test_settings, 910002)
    await create_budget(user, period="10.09.2026 — 16.09.2026", repeat="недел")
    await _post(user, "продукты 500")

    await user.send("Сколько потрачено за месяц?")
    text = user.text()
    assert "короче месяца" in text
    assert user.has_button("Календарный месяц")
    assert user.has_button("Текущий период")

    await user.press(user.button_data("Календарный месяц"))
    assert "Разрез: календарный месяц" in user.text()


async def test_a86_ai_free_answer_matches_snapshot(bot: None, test_settings: Settings) -> None:
    """A86: числовой ответ содержит период и ссылку на детализацию."""
    user = make_user(test_settings, 910003)
    await create_budget(user)
    await _post(user, "продукты 1500")
    await user.send("Сколько потрачено?")
    text = user.text()
    assert "Разрез:" in text
    assert "1 500" in text.replace(" ", " ").replace(" ", " ")
    assert user.has_button("Детализация")


async def test_a91_incomplete_history_gives_no_confident_permission(
    bot: None, test_settings: Settings
) -> None:
    """A91: при неполной истории показывается остаток плана без разрешения тратить."""
    user = make_user(test_settings, 910004)
    await create_budget(user, limits="Продукты 20000")
    await _post(user, "продукты 1500")
    await user.send("/report")
    text = user.text()
    assert "Ограничения:" in text
    assert "можете потратить" not in text.lower()
    assert "полнота" in text.lower()


async def test_spender_slice_requires_linked_profile(bot: None, test_settings: Settings) -> None:
    """FR-58: разрез «кто потратил» не выдумывается без привязанного профиля."""
    user = make_user(test_settings, 910005)
    await create_budget(user)
    await _post(user, "продукты 500")
    await user.send("Сколько я потратил?")
    assert user.has_button("Я потратил")
    await user.press(user.button_data("Я потратил"))
    text = user.text()
    assert "Разрез:" in text or "не привязан" in text
