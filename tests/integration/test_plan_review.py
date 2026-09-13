"""Опережение обзора плана и взнос в фонд (FORM-05, FORM-10, FR-52, A228)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.commitments.goals import suggested_contribution
from fintracker.application.planning.rollover import (
    plan_review_date,
    plan_review_lead_days,
)
from fintracker.core.money import Money
from tests.conftest import requires_pg

pytestmark = [pytest.mark.pg, requires_pg]


def test_form10_lead_days_by_period_length() -> None:
    """FORM-10: опережение равно min(3, длительность − 1)."""
    assert plan_review_lead_days(30) == 3
    assert plan_review_lead_days(14) == 3
    assert plan_review_lead_days(4) == 3
    assert plan_review_lead_days(3) == 2
    assert plan_review_lead_days(2) == 1
    # Однодневный период: отдельного опережения нет (A228).
    assert plan_review_lead_days(1) == 0
    assert plan_review_lead_days(0) == 0


def test_form10_review_date_stays_inside_period() -> None:
    """FORM-10: дата обзора не выходит за границы текущего периода."""
    month = plan_review_date(start_date=dt.date(2026, 9, 10), end_exclusive=dt.date(2026, 10, 10))
    assert month == dt.date(2026, 10, 6)

    two_days = plan_review_date(start_date=dt.date(2026, 9, 10), end_exclusive=dt.date(2026, 9, 12))
    assert two_days == dt.date(2026, 9, 10)

    single = plan_review_date(start_date=dt.date(2026, 9, 10), end_exclusive=dt.date(2026, 9, 11))
    assert single == dt.date(2026, 9, 10)


def test_form05_contribution_is_rounded_up() -> None:
    """FORM-05: взнос равен округлению вверх недостающей суммы."""
    value = suggested_contribution(
        target=Money(100_000, "RUB"),
        already_allocated=Money(0, "RUB"),
        remaining_contributions=3,
    )
    assert value.minor == 33_334

    exact = suggested_contribution(
        target=Money(90_000, "RUB"),
        already_allocated=Money(0, "RUB"),
        remaining_contributions=3,
    )
    assert exact.minor == 30_000

    # Взнос не превышает недостающую сумму.
    last = suggested_contribution(
        target=Money(100_000, "RUB"),
        already_allocated=Money(99_000, "RUB"),
        remaining_contributions=5,
    )
    assert last.minor == 200

    reached = suggested_contribution(
        target=Money(100_000, "RUB"),
        already_allocated=Money(100_000, "RUB"),
        remaining_contributions=2,
    )
    assert reached.minor == 0


async def test_a228_single_day_period_has_no_separate_review_job(
    clean_db: None, test_settings, owner_session: AsyncSession
) -> None:
    """A228: для однодневного периода отдельное задание обзора не ставится."""
    from fintracker.application.planning.rollover import handle_open_next_period
    from fintracker.db.models.planning import PeriodPolicyRow
    from fintracker.db.models.platform import Job
    from tests.integration.factories import build_fixture

    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 10))
    policy = (
        await owner_session.execute(
            select(PeriodPolicyRow).where(PeriodPolicyRow.workspace_id == fixture.workspace.id)
        )
    ).scalar_one()
    policy.mode = "fixed_days"
    policy.interval = 1
    policy.first_end_exclusive = dt.date(2026, 9, 11)
    await owner_session.flush()
    await owner_session.commit()

    from fintracker.application.platform import queue
    from fintracker.db.session import RuntimeRole as Role
    from fintracker.db.session import session_scope as scope

    async with scope(test_settings, Role.WORKER, workspace_id=fixture.workspace.id) as session:
        await queue.enqueue(
            session,
            job_type="open_next_period",
            logical_key=f"open_period:{fixture.workspace.id}:2026-09-12",
            queue_class="calendar",
            workspace_id=fixture.workspace.id,
            payload={"local_date": dt.date(2026, 9, 12).isoformat(), "schema_version": 1},
            correlation_id="test-review",
        )
    jobs = await queue.claim_jobs(test_settings, queue_classes=("calendar",), limit=5)
    job = next(item for item in jobs if item.job_type == "open_next_period")
    await handle_open_next_period(test_settings, job)

    from fintracker.db.session import RuntimeRole, session_scope

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        jobs = (
            (await session.execute(select(Job).where(Job.job_type == "plan_review")))
            .scalars()
            .all()
        )
    assert jobs == [], "однодневный период не создаёт отдельное задание обзора"
