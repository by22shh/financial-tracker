"""UX-01/UX-14: financial inputs always name and preserve their period."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from fintracker.application.planning.plan import current_budget_version
from fintracker.config import Settings
from fintracker.db.models.catalog import Category
from fintracker.db.models.planning import (
    BudgetLine,
    BudgetPeriod,
    IncomePlan,
    PeriodPolicyRow,
)
from fintracker.db.session import RuntimeRole, session_scope
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


async def test_next_plan_limit_editor_changes_only_named_future_period(
    bot: None, test_settings: Settings
) -> None:
    user = make_user(test_settings, 929040)
    await create_budget(user, categories="Продукты", limits="Продукты = 20000")

    await user.send("/budget")
    await user.press(user.button_data("Следующий период"))
    assert "10 октября — 9 ноября" in user.text()
    await user.press(user.button_data("Изменить лимиты"))
    assert "Лимиты на 10 октября — 9 ноября" in user.text()
    assert "Текущий период останется без изменений" in user.text()
    await user.press(user.button_data("Продукты"))
    assert "Период: 10 октября — 9 ноября" in user.text()
    await user.send("25000")
    assert "Лимит на 10 октября — 9 ноября обновлён" in user.text()
    assert "Текущий период не изменился" in user.text()

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        category_id = (
            await session.execute(select(Category.id).where(Category.name == "Продукты"))
        ).scalar_one()
        periods = (
            (await session.execute(select(BudgetPeriod).order_by(BudgetPeriod.start_date)))
            .scalars()
            .all()
        )
        current, following = periods[0], periods[1]
        current_version = await current_budget_version(
            session, workspace_id=current.workspace_id, period_id=current.id
        )
        following_version = await current_budget_version(
            session, workspace_id=following.workspace_id, period_id=following.id
        )
        assert current_version is not None
        assert following_version is not None

        async def limit_for(version_id: uuid.UUID) -> int | None:
            return (
                await session.execute(
                    select(BudgetLine.limit_minor).where(
                        BudgetLine.budget_version_id == version_id,
                        BudgetLine.category_id == category_id,
                        BudgetLine.beneficiary_id.is_(None),
                    )
                )
            ).scalar_one()

        assert await limit_for(current_version.id) == 2_000_000
        assert await limit_for(following_version.id) == 2_500_000


@pytest.mark.parametrize(
    ("dates", "repeat_button", "interval"),
    [
        ("10.09.2026 — 16.09.2026", "каждую неделю", 7),
        ("10.09.2026 — 19.09.2026", "каждые 10 дней", 10),
    ],
)
async def test_non_monthly_income_is_saved_for_explicit_first_period(
    bot: None,
    test_settings: Settings,
    dates: str,
    repeat_button: str,
    interval: int,
) -> None:
    user = make_user(test_settings, 929050 + interval)
    await user.send("/start")
    await user.press("wiz:start")
    for value in ("Недельный бюджет", "RUB", "Asia/Novosibirsk", dates):
        await user.send(value)
    await user.press(user.button_data(repeat_button))

    assert "за первый период" in user.text()
    assert dates in user.text()
    assert "за месяц" not in user.text()
    await user.press("wiz:inc:exact")
    assert "за первый период" in user.text()
    assert dates in user.text()
    await user.send("70000")
    await user.send("Продукты")
    for step in ("limits", "commitments", "goals"):
        await user.press(f"wiz:skip:{step}")
    await user.press("wiz:tpl:on")
    assert "70\u00a0000" in user.text()
    await user.press("wiz:publish")

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        plan = (await session.execute(select(IncomePlan))).scalar_one()
        policy = (await session.execute(select(PeriodPolicyRow))).scalar_one()
        assert plan.basis == "period_total"
        assert plan.monthly_amount_minor is None
        assert plan.period_amount_minor == 7_000_000
        assert plan.expected_minor == 7_000_000
        assert policy.mode == "fixed_days"
        assert policy.interval == interval
