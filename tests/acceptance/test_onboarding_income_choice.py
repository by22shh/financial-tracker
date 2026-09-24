"""Выбор точности дохода должен вести к запросу суммы, а не повторять выбор."""

import pytest
from sqlalchemy import select

from fintracker.config import Settings
from fintracker.db.models.planning import IncomePlan
from fintracker.db.session import RuntimeRole, session_scope
from tests.acceptance.conftest import BotUser, make_user
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def reach_income(user: BotUser) -> None:
    await user.send("/start")
    await user.press("wiz:start")
    for value in ("Личный", "RUB", "Asia/Novosibirsk", "10.09.2026 — 09.10.2026"):
        await user.send(value)
    await user.press(user.button_data("календарный месяц"))


@pytest.mark.parametrize("precision", ["exact", "estimate"])
async def test_income_choice_requests_amount_and_survives_resume(
    bot: None, test_settings: Settings, precision: str
) -> None:
    user = make_user(test_settings, 929010)
    await reach_income(user)
    before = user.text()
    await user.press(f"wiz:inc:{precision}")
    assert user.text() != before
    assert "Отправьте сумму дохода" in user.text()
    assert "RUB" in user.text()
    assert not user.has_button("Точный план")
    assert not user.has_button("Примерная оценка")
    selected = user.text()
    await user.send("/start")
    await user.press(user.button_data("Продолжить настройку"))
    assert user.text() == selected
    await user.send("не знаю сумму")
    assert "Укажите сумму" in user.text()
    await user.send("400000")
    assert "Категории расходов" in user.text()
    # Старое сообщение с кнопками не откатывает мастер и не меняет точность.
    await user.press("wiz:inc:estimate" if precision == "exact" else "wiz:inc:exact")
    assert "Категории расходов" in user.text()
    await user.send("Продукты")
    for step in ("limits", "commitments", "goals"):
        await user.press(f"wiz:skip:{step}")
    await user.press("wiz:tpl:on")
    assert "400\u00a0000" in user.text()
    await user.press("wiz:publish")
    assert "создан!" in user.text()
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        plan = (await session.execute(select(IncomePlan))).scalar_one()
        assert plan.precision == precision
        assert plan.monthly_amount_minor == 40_000_000
        assert plan.period_amount_minor == 40_000_000


async def test_income_precision_can_be_changed_or_skipped(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 929011)
    await reach_income(user)
    await user.press("wiz:inc:exact")
    await user.press(user.button_data("Сделать примерным"))
    assert "Выбрана примерная оценка" in user.text()
    await user.press(user.button_data("Сделать точным"))
    assert "Выбран точный план" in user.text()
    await user.send("400000")
    await user.press("wiz:back")
    await user.press(user.button_data("Укажу позже"))
    assert "Категории расходов" in user.text()
    await user.send("Продукты")
    for step in ("limits", "commitments", "goals"):
        await user.press(f"wiz:skip:{step}")
    await user.press("wiz:tpl:on")
    assert "Доход: не указан" in user.text()
    await user.press("wiz:publish")
    assert "создан!" in user.text()
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        assert (await session.execute(select(IncomePlan))).scalars().all() == []
