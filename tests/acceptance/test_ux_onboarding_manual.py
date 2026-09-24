"""Acceptance coverage for the mobile-friendly onboarding and manual flows."""

from __future__ import annotations

import pytest

from fintracker.config import Settings
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_wizard_shows_progress_back_and_review_field_edit(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 928001)
    await user.send("/start")
    await user.press("wiz:start")
    assert "Шаг 1 из 11" in user.text()
    await user.send("Семейный")
    assert "Шаг 2 из 11" in user.text() and user.has_button("Назад")
    await user.press(user.button_data("Назад"))
    assert "Шаг 1 из 11" in user.text()
    await user.send("Семейный")
    await user.press("wiz:cur:RUB")
    await user.press("wiz:tz:nsk")
    await user.press("wiz:period:month")
    await user.press("wiz:inc:later")
    await user.send("Продукты")
    await user.press("wiz:skip:limits")
    await user.press("wiz:skip:commitments")
    await user.press("wiz:skip:goals")
    await user.press("wiz:tpl:on")
    assert "Шаг 11 из 11" in user.text()
    await user.press("wiz:edit:name")
    assert "Шаг 1 из 11" in user.text()
    await user.send("Семейный 2")
    assert "Проверьте настройки" in user.text()
    assert "Бюджет: Семейный 2" in user.text()


async def test_timezone_and_period_offer_presets_and_custom_input(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 928002)
    await user.send("/start")
    await user.press("wiz:start")
    await user.send("Тест")
    await user.press("wiz:cur:RUB")
    assert user.has_button("Москва") and user.has_button("Новосибирск")
    await user.press(user.button_data("Другой часовой пояс"))
    assert "Asia/Irkutsk" in user.text()
    await user.send("Asia/Irkutsk")
    assert user.has_button("Календарный месяц")
    assert user.has_button("Неделя") and user.has_button("С 10-го по 9-е")
    await user.press(user.button_data("Свои даты"))
    assert "10.09.2026 — 09.10.2026" in user.text()
    await user.send("10.09.2026 — 09.10.2026")
    assert "Повторение бюджета" in user.text()


async def test_payment_and_goal_are_collected_in_short_steps(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 928003)
    await create_budget(user, categories="Продукты")

    await user.press("pay:new")
    assert "Как он называется" in user.text()
    await user.send("Интернет")
    assert "Какая сумма" in user.text()
    await user.send("900")
    assert "Когда платить" in user.text()
    await user.send("20.09.2026")
    assert "Как часто платить" in user.text()
    await user.press(user.button_data("Каждый месяц"))
    assert "Платёж «Интернет»" in user.text()

    await user.press("goal:new")
    assert "На что хотите накопить" in user.text()
    await user.send("Отпуск")
    assert "Какая сумма нужна" in user.text()
    await user.send("100000")
    assert "Цель «Отпуск»" in user.text()


async def test_manual_expense_form_needs_no_separators(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 928004)
    await create_budget(user, categories="Продукты")
    await user.send("/add")
    await user.send("1200")
    assert "Категория для" in user.text() and user.has_button("Продукты")
    await user.send("Продукты")
    assert "Когда была трата" in user.text() and user.has_button("Вчера")
    await user.send("сегодня")
    assert "Комментарий" in user.text()
    await user.send("ужин")
    assert "Расход записан" in user.text()
    assert "Продукты" in user.text()
