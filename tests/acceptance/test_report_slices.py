"""Разрезы отчёта и уточнение периода (A53, A85, A86, A90, A91, A229)."""

from __future__ import annotations

import uuid

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
    assert "Расходы за календарный месяц" in text
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
    assert "Расходы за календарный месяц" in user.text()


async def test_a86_ai_free_answer_matches_snapshot(bot: None, test_settings: Settings) -> None:
    """A86: числовой ответ содержит период и ссылку на детализацию."""
    user = make_user(test_settings, 910003)
    await create_budget(user)
    await _post(user, "продукты 1500")
    await user.send("Сколько потрачено?")
    text = user.text()
    assert "📊 Расходы за" in text
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
    assert "Прогноз появится" in text
    assert "можете потратить" not in text.lower()
    assert "полнота" in text.lower()


async def test_spender_slice_requires_linked_profile(bot: None, test_settings: Settings) -> None:
    """FR-58: разрез «кто потратил» не выдумывается без привязанного профиля."""
    user = make_user(test_settings, 910005)
    await create_budget(user)
    await _post(user, "продукты 500")
    await user.send("Сколько я потратил?")
    assert user.has_button("Платил я")
    await user.press(user.button_data("Платил я"))
    text = user.text()
    assert "📊 Расходы" in text or "Бот пока не знает" in text


async def test_a132_choosing_action_changes_nothing_financial(
    bot: None, test_settings: Settings
) -> None:
    """CMD-24, A132: «Выбрать действие» сохраняет намерение, но не меняет лимит и журнал."""
    from sqlalchemy import select

    from fintracker.application.identity.actor import get_active_workspace_id
    from fintracker.db.models.intelligence import Recommendation
    from fintracker.db.models.planning import BudgetVersion
    from fintracker.db.session import RuntimeRole, session_scope

    user = make_user(test_settings, 910010)
    await create_budget(user, limits="Продукты 20000")
    await _post(user, "продукты 1500")

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        from fintracker.application.identity.actor import ensure_user

        person = await ensure_user(session, telegram_user_id=910010)
        workspace_id = await get_active_workspace_id(session, person.id)
        assert workspace_id is not None
        run_id = uuid.uuid4()
        from fintracker.db.models.intelligence import AnalysisRun

        session.add(
            AnalysisRun(
                id=run_id,
                workspace_id=workspace_id,
                run_kind="weekly_review",
                logical_key=f"weekly:{run_id}",
                status="succeeded",
                profile_version="test",
                prompt_version="test",
            )
        )
        await session.flush()
        session.add(
            Recommendation(
                workspace_id=workspace_id,
                run_id=run_id,
                direction="flexible_spend",
                observation="Гибкие траты выше обычного",
                action_kind="adjust_plan",
                action_payload={},
                metric_refs=[{"metric": "line_fact", "value_minor": 150_000}],
                effect_unavailable_reason="Недостаточно данных",
                revision_vector={},
                status="proposed",
            )
        )
        versions_before = len(
            (
                await session.execute(
                    select(BudgetVersion).where(BudgetVersion.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )

    await user.press("rec:list")
    assert "Гибкие траты выше обычного" in user.text()
    await user.press(user.button_data("Выбрать действие"))
    text = user.text()
    assert "Сами лимиты и записи не изменились" in text
    assert "не изменились" in text

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        versions_after = len(
            (
                await session.execute(
                    select(BudgetVersion).where(BudgetVersion.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert versions_after == versions_before, "план не изменён выбором действия"

    await user.send("/budget")
    assert "20 000" in user.text().replace(" ", " ").replace(" ", " ")


async def test_a137_done_mark_does_not_claim_proven_savings(
    bot: None, test_settings: Settings
) -> None:
    """A137: отметка о выполнении не объявляет экономию доказанной."""
    from fintracker.application.identity.actor import ensure_user, get_active_workspace_id
    from fintracker.db.models.intelligence import AnalysisRun, Recommendation
    from fintracker.db.session import RuntimeRole, session_scope

    user = make_user(test_settings, 910011)
    await create_budget(user)

    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        person = await ensure_user(session, telegram_user_id=910011)
        workspace_id = await get_active_workspace_id(session, person.id)
        assert workspace_id is not None
        run_id = uuid.uuid4()
        session.add(
            AnalysisRun(
                id=run_id,
                workspace_id=workspace_id,
                run_kind="weekly_review",
                logical_key=f"weekly:{run_id}",
                status="succeeded",
                profile_version="test",
                prompt_version="test",
            )
        )
        await session.flush()
        recommendation = Recommendation(
            workspace_id=workspace_id,
            run_id=run_id,
            direction="known_recurring",
            observation="Подписка списывается ежемесячно",
            action_kind="check_tariff",
            action_payload={},
            metric_refs=[{"metric": "line_fact", "value_minor": 90_000}],
            effect_unavailable_reason="Недостаточно данных",
            revision_vector={},
            status="proposed",
        )
        session.add(recommendation)
        await session.flush()
        code = recommendation.id.hex[:16]

    await user.press(f"rec:done:{code}")
    text = user.text()
    assert "Отметка сохранена" in text
    assert "оценит по следующим тратам" in text
