"""Independent readiness probes; assertions describe the required result."""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select, func

from fintracker.application.analytics.coverage import record_reconciliation, accept_reconciliation
from fintracker.application.commitments.schedules import create_schedule, materialize_occurrences, settle_occurrence
from fintracker.application.conversation.context import current_status
from fintracker.application.conversation.analytics_flow import report_view
from fintracker.application.ledger.operations import post_mixed_payment, settle_receivable, post_refund, refundable_parts
from fintracker.application.ledger.service import post_transaction, void_transaction, revise_transaction, load_current_spec, account_balance
from fintracker.application.planning.plan import current_budget_version, period_status
from fintracker.application.planning.rollover import handle_open_next_period
from fintracker.application.platform.queue import LeasedJob
from fintracker.core.errors import ConflictError
from fintracker.db.models.ledger import Receivable, Allocation
from fintracker.db.models.commitments import Occurrence, Reconciliation
from fintracker.db.models.planning import BudgetPeriod, BudgetLine
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.schedule import ScheduleRule, ScheduleKind
from tests.integration.factories import build_fixture, TZ
from tests.integration.test_money_scenarios import expense_spec, rub
from tests.integration.test_period_automation import _add_template

pytestmark = pytest.mark.pg
DAY = dt.date(2026, 9, 12)

@asynccontextmanager
async def command(settings, fixture):
    async with session_scope(settings, RuntimeRole.API, user_id=fixture.user.id, workspace_id=fixture.workspace.id) as session:
        uow = UnitOfWork(session, "readiness-money")
        await uow.lock_workspace(fixture.workspace.id, actor=fixture.actor)
        yield session, uow

async def test_read_opened_period_still_gets_plan_and_closure(owner_session, test_settings):
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10), limits={"Продукты": 100000})
    await _add_template(owner_session, fixture, limits={"Продукты": 100000})
    await owner_session.commit()
    # Exact production caller used by /budget, report, and post-save status.
    status = await current_status(test_settings, actor=fixture.actor, workspace=fixture.workspace)
    job = LeasedJob(id=uuid.uuid4(), job_type="open_next_period", queue_class="calendar", workspace_id=fixture.workspace.id, subject_id=None, payload={}, payload_version=1, attempts=1, max_attempts=6, lease_token=uuid.uuid4(), lease_until=dt.datetime.now(dt.UTC)+dt.timedelta(minutes=5), deadline_at=None, correlation_id="readiness-money", logical_key="readiness-money-open")
    await handle_open_next_period(test_settings, job)
    async with command(test_settings, fixture) as (session, _):
        plan = await current_budget_version(session, workspace_id=fixture.workspace.id, period_id=status.period_id)
        previous = await session.get(BudgetPeriod, fixture.period.id)
        assert (plan is not None, previous.state) == (True, "ended"), ("period created by normal read; worker failed to finish initialization", plan, previous.state)

async def test_void_settlement_reopens_receivable(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        await post_mixed_payment(session, uow, actor=fixture.actor, total=rub(3000), own_share=rub(1500), counterparty_label="Друг", counterparty_person_id=None, category_id=fixture.categories["Продукты"], beneficiary_id=None, occurred_date=DAY, timezone=TZ, account_id=fixture.accounts["Карта"])
        receivable = (await session.execute(select(Receivable))).scalar_one()
        settlement = await settle_receivable(session, uow, actor=fixture.actor, receivable_id=receivable.id, amount=rub(1500), occurred_date=DAY, timezone=TZ, account_id=fixture.accounts["Карта"])
    async with command(test_settings, fixture) as (session, uow):
        await void_transaction(session, uow, actor=fixture.actor, transaction_id=settlement.transaction_id, expected_version=settlement.entity_version)
    async with command(test_settings, fixture) as (session, _):
        remaining = (await session.execute(select(Receivable))).scalar_one()
        balance = await account_balance(session, workspace_id=fixture.workspace.id, account_id=fixture.accounts["Карта"])
        assert (remaining.outstanding_minor, remaining.status, balance) == (150000, "open", -300000)

async def test_void_paid_purchase_cannot_leave_collected_receivable(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase = await post_mixed_payment(session, uow, actor=fixture.actor, total=rub(3000), own_share=rub(1500), counterparty_label="Друг", counterparty_person_id=None, category_id=fixture.categories["Продукты"], beneficiary_id=None, occurred_date=DAY, timezone=TZ)
        receivable = (await session.execute(select(Receivable))).scalar_one()
        await settle_receivable(session, uow, actor=fixture.actor, receivable_id=receivable.id, amount=rub(1500), occurred_date=DAY, timezone=TZ)
    with pytest.raises(ConflictError, match="."):
        async with command(test_settings, fixture) as (session, uow):
            await void_transaction(session, uow, actor=fixture.actor, transaction_id=purchase.transaction_id, expected_version=purchase.entity_version)

async def test_void_scheduled_payment_reopens_obligation(owner_session, test_settings):
    fixture = await build_fixture(owner_session, limits={"Продукты": 100000})
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        await create_schedule(session, uow, actor=fixture.actor, name="Регулярный платёж", direction="payment", rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=DAY), currency="RUB", expected=rub(1000), category_id=fixture.categories["Продукты"])
        occurrence = (await materialize_occurrences(session, workspace_id=fixture.workspace.id, until_date=DAY))[0]
        purchase = await post_transaction(session, uow, actor=fixture.actor, spec=expense_spec(fixture, amount=rub(1000), category="Продукты"), origin="form")
        await settle_occurrence(session, uow, actor=fixture.actor, occurrence_id=occurrence.id, transaction_id=purchase.transaction_id, effect_id=purchase.effect_id, amount=rub(1000))
    async with command(test_settings, fixture) as (session, uow):
        await void_transaction(session, uow, actor=fixture.actor, transaction_id=purchase.transaction_id, expected_version=purchase.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Occurrence, occurrence.id)
        status = await period_status(session, workspace_id=fixture.workspace.id, period_id=fixture.period.id, currency="RUB", today=DAY)
        commitment = sum(line.commitments_minor for line in status.lines)
        assert (row.state, row.settled_minor, commitment) == ("planned", 0, 100000)

async def test_purchase_reallocation_cannot_shrink_refunded_part(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    base = expense_spec(fixture, amount=rub(1000), category="Продукты")
    first = replace(base.allocations[0], amount=rub(600))
    second = replace(first, stable_line_id=uuid.uuid4(), amount=rub(400), category_id=fixture.categories["Рестораны"])
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase = await post_transaction(session, uow, actor=fixture.actor, spec=replace(base, allocations=(first, second)), origin="form")
        await post_refund(session, uow, actor=fixture.actor, source_transaction_id=purchase.transaction_id, parts={first.stable_line_id: rub(600)}, occurred_date=DAY, timezone=TZ)
    with pytest.raises(ConflictError, match="."):
        async with command(test_settings, fixture) as (session, uow):
            await revise_transaction(session, uow, actor=fixture.actor, transaction_id=purchase.transaction_id, new_spec=replace(base, allocations=(replace(first, amount=rub(100)), replace(second, amount=rub(900)))), expected_version=purchase.entity_version)

async def test_void_transaction_invalidates_accepted_reconciliation(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase = await post_transaction(session, uow, actor=fixture.actor, spec=expense_spec(fixture, amount=rub(1000), category="Продукты", account="Карта"), origin="form")
        reconciliation = await record_reconciliation(session, uow, actor=fixture.actor, account_id=fixture.accounts["Карта"], cutoff_date=DAY, observed=rub(-1000))
        await accept_reconciliation(session, uow, actor=fixture.actor, reconciliation_id=reconciliation.reconciliation_id, adjust=False)
    async with command(test_settings, fixture) as (session, uow):
        await void_transaction(session, uow, actor=fixture.actor, transaction_id=purchase.transaction_id, expected_version=purchase.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Reconciliation, reconciliation.reconciliation_id)
        balance = await account_balance(session, workspace_id=fixture.workspace.id, account_id=fixture.accounts["Карта"])
        assert (row.is_stale, balance) == (True, 0)

async def test_reconciliation_accept_revalidates_changed_basis(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        reconciliation = await record_reconciliation(session, uow, actor=fixture.actor, account_id=fixture.accounts["Карта"], cutoff_date=DAY, observed=rub(1000))
    async with command(test_settings, fixture) as (session, uow):
        await post_transaction(session, uow, actor=fixture.actor, spec=expense_spec(fixture, amount=rub(200), category="Продукты", account="Карта"), origin="form")
    with pytest.raises(ConflictError, match="."):
        async with command(test_settings, fixture) as (session, uow):
            await accept_reconciliation(session, uow, actor=fixture.actor, reconciliation_id=reconciliation.reconciliation_id, adjust=True, reason="User accepted earlier preview")

async def test_incomplete_calendar_days_are_not_observed_forecast_days(owner_session, test_settings):
    fixture = await build_fixture(owner_session, start=dt.date(2026, 9, 1))
    await post_transaction(owner_session, fixture.uow, actor=fixture.actor, spec=expense_spec(fixture, amount=rub(1000), category="Продукты"), origin="form")
    await owner_session.commit()
    replies = await report_view(test_settings, actor=fixture.actor, workspace=fixture.workspace)
    text = replies[0].text
    assert "Числовой прогноз не строится" in text, text
