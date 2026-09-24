"""Real PostgreSQL regressions from the Telegram audit TG-03."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from fintracker.application.analytics.reviews import build_next_period_draft
from fintracker.application.commitments.schedules import materialize_occurrences
from fintracker.application.conversation.payments_flow import (
    create_payment_from_text,
    payment_action,
    payments_view,
)
from fintracker.application.delivery.render import format_date
from fintracker.application.onboarding.wizard import WizardState, _create_planned
from fintracker.application.planning.periods import ensure_periods
from fintracker.db.models.commitments import Occurrence, ScheduleVersion
from fintracker.db.models.planning import BudgetPeriod
from fintracker.db.session import RuntimeRole, session_scope
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture

pytestmark = [pytest.mark.pg, requires_pg]


async def test_new_payment_uses_upcoming_yearless_date(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    today = dt.datetime.now(ZoneInfo(fixture.workspace.timezone)).date()
    due = today + dt.timedelta(days=7)
    replies = await create_payment_from_text(
        test_settings,
        actor=fixture.actor,
        workspace=fixture.workspace,
        text=f"Интернет = 900 = {due:%d.%m}",
    )
    assert format_date(due, with_year=True) in replies[0].text
    # Платёж создаётся после выбора повторения: бот не решает за человека.
    await payment_action(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, action="rep", rest=["m"]
    )
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        version = (await session.execute(select(ScheduleVersion))).scalar_one()
        assert version.anchor_date == due


async def test_onboarding_payment_has_no_phantom_arrears(owner_session):
    fixture = await build_fixture(owner_session)
    state = WizardState(
        start_date=dt.date(2026, 9, 10),
        commitments=[{"name": "ТЕСТ Интернет", "amount_decimal": "900", "due": "25.09"}],
    )
    await _create_planned(
        owner_session, fixture.uow, workspace=fixture.workspace, actor=fixture.actor, state=state
    )
    version = (await owner_session.execute(select(ScheduleVersion))).scalar_one()
    assert version.anchor_date == dt.date(2026, 9, 25)
    await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 9, 30)
    )
    occurrences = (await owner_session.execute(select(Occurrence))).scalars().all()
    assert [item.due_date for item in occurrences] == [dt.date(2026, 9, 25)]
    await ensure_periods(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 10)
    )
    next_period = (
        await owner_session.execute(
            select(BudgetPeriod).where(BudgetPeriod.start_date == dt.date(2026, 10, 10))
        )
    ).scalar_one()
    draft = await build_next_period_draft(
        owner_session,
        workspace=fixture.workspace,
        period_id=next_period.id,
        today=dt.date(2026, 9, 20),
    )
    assert draft.commitments_minor == 180_000  # September arrears + October payment.
    occurrences = (await owner_session.execute(select(Occurrence))).scalars().all()
    assert sorted(item.due_date for item in occurrences) == [
        dt.date(2026, 9, 25),
        dt.date(2026, 10, 25),
    ]
    september = next(item for item in occurrences if item.due_date.month == 9)
    september.settled_minor = 90_000
    september.state = "settled"
    # The first day AFTER the period must not be counted in its plan.
    october = next(item for item in occurrences if item.due_date.month == 10)
    october.due_date = next_period.end_exclusive
    await owner_session.flush()
    draft = await build_next_period_draft(
        owner_session,
        workspace=fixture.workspace,
        period_id=next_period.id,
        today=dt.date(2026, 9, 20),
    )
    assert draft.commitments_minor == 0


async def test_payment_pages_show_all_dates_and_allow_return(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    today = dt.datetime.now(dt.UTC).date()
    # Explicit old year intentionally preserves genuine arrears.
    state = WizardState(
        start_date=today,
        commitments=[
            {"name": "Интернет", "amount_decimal": "900", "due": f"01.01.{today.year - 1}"}
        ],
    )
    await _create_planned(
        owner_session, fixture.uow, workspace=fixture.workspace, actor=fixture.actor, state=state
    )
    await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=today
    )
    dates = sorted((await owner_session.execute(select(Occurrence.due_date))).scalars().all())
    await owner_session.commit()
    first = await payments_view(test_settings, actor=fixture.actor, workspace=fixture.workspace)
    assert "Страница 1 из" in first[0].text
    assert format_date(dates[0], with_year=dates[0].year != today.year) in first[0].text
    assert "просрочен" in first[0].text
    second = await payment_action(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, action="page", rest=["1"]
    )
    assert format_date(dates[6], with_year=dates[6].year != today.year) in second[0].text
    assert format_date(dates[0], with_year=dates[0].year != today.year) not in second[0].text
    last = await payments_view(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, page=999
    )
    assert format_date(dates[-1], with_year=dates[-1].year != today.year) in last[0].text
    assert not any(button.text == "Далее →" for row in last[0].buttons for button in row)
