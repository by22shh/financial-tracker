"""Категория → сумма → список → публикация; настройка переживает продолжение."""

import pytest
from sqlalchemy import select

from fintracker.config import Settings
from fintracker.db.models.catalog import Category
from fintracker.db.models.planning import BudgetLine
from fintracker.db.session import RuntimeRole, session_scope
from tests.acceptance.conftest import make_user
from tests.acceptance.test_onboarding_income_choice import reach_income
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_button_limits_survive_resume_and_publish(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 929020)
    await reach_income(user)
    await user.press("wiz:inc:later")
    await user.send("Продукты, Кафе, Транспорт")
    products_button = user.button_data("Продукты")
    await user.press(products_button)
    assert "Лимит: Продукты" in user.text()
    await user.send("/start")
    await user.press(user.button_data("Продолжить настройку"))
    assert "Лимит: Продукты" in user.text()
    await user.send("не число")
    assert user.has_button("К категориям")
    await user.send("15000")
    assert "сохранён" in user.text()
    assert user.has_button("Продукты · 15")
    await user.press(user.button_data("Кафе"))
    await user.send("0")
    assert user.has_button("Кафе · 0")
    await user.press(user.button_data("Транспорт"))
    await user.send("3000")
    await user.press(user.button_data("Транспорт"))
    await user.press(user.button_data("Без лимита"))
    assert user.has_button("Транспорт · без лимита")
    await user.press(user.button_data("Готово"))
    assert "Регулярные платежи" in user.text()
    await user.press(products_button)
    assert "Регулярные платежи" in user.text()
    await user.press("wiz:skip:limits")
    assert "Регулярные платежи" in user.text()
    await user.press("wiz:skip:commitments")
    await user.press("wiz:skip:goals")
    await user.press("wiz:tpl:on")
    assert "15\u00a0000" in user.text()
    await user.press("wiz:publish")
    assert "создан!" in user.text()
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        rows = (
            await session.execute(
                select(Category.name, BudgetLine.limit_minor).join(
                    BudgetLine, BudgetLine.category_id == Category.id
                )
            )
        ).all()
        assert dict(rows) == {"Продукты": 1500000, "Кафе": 0, "Транспорт": None}


async def test_second_page_and_back_keep_existing_limit(bot: None, test_settings: Settings) -> None:
    user = make_user(test_settings, 929021)
    await reach_income(user)
    await user.press("wiz:inc:later")
    await user.press("wiz:cats:template")
    assert "Страница 1 из 2" in user.text()
    await user.press(user.button_data("Далее"))
    assert "Страница 2 из 2" in user.text()
    await user.press(user.button_data("Одежда"))
    await user.send("2000")
    await user.press(user.button_data("Одежда"))
    await user.press(user.button_data("К категориям"))
    assert user.has_button("Одежда · 2")
    assert "Страница 2 из 2" in user.text()


async def test_categories_from_scratch_and_plain_amount_need_selection(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 929022)
    await reach_income(user)
    await user.press("wiz:inc:later")
    await user.press("wiz:cats:empty")
    assert "Перечислите" in user.text()
    await user.send("Кофе, Дом")
    await user.send("500")
    assert "Сначала выберите категорию" in user.text()
    await user.press(user.button_data("Дом"))
    await user.send("500")
    assert user.has_button("Дом · 500")
    assert user.has_button("Кофе · без лимита")
