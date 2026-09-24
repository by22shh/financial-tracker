"""Additional recheck assertions: required behavior beyond the frozen audit cases."""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select

from fintracker.application.analytics.coverage import accept_reconciliation, record_reconciliation
from fintracker.application.analytics.reviews import build_weekly_review
from fintracker.application.commitments.schedules import (
    create_schedule,
    materialize_occurrences,
    settle_occurrence,
)
from fintracker.application.ledger.operations import post_mixed_payment, settle_receivable
from fintracker.application.ledger.service import (
    account_balance,
    load_current_spec,
    post_transaction,
    restore_transaction,
    revise_transaction,
    void_transaction,
)
from fintracker.application.planning.periods import period_for_date
from fintracker.application.planning.rollover import apply_plan_for_period, handle_open_next_period
from fintracker.application.platform.queue import LeasedJob
from fintracker.core.errors import ConflictError
from fintracker.db.models.commitments import Occurrence, Reconciliation
from fintracker.db.models.ledger import Receivable
from fintracker.db.models.planning import BudgetPeriod
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.schedule import ScheduleKind, ScheduleRule
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub
from tests.integration.test_period_automation import _add_template

pytestmark = pytest.mark.pg
DAY = dt.date(2026, 9, 12)


@asynccontextmanager
async def command(settings, fixture):
    async with session_scope(
        settings, RuntimeRole.API, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    ) as session:
        uow = UnitOfWork(session, "money-recheck-5646887")
        await uow.lock_workspace(fixture.workspace.id, actor=fixture.actor)
        yield session, uow


async def mixed(session, uow, fixture, *, collect=True):
    purchase = await post_mixed_payment(
        session,
        uow,
        actor=fixture.actor,
        total=rub(3000),
        own_share=rub(1500),
        counterparty_label="Друг",
        counterparty_person_id=None,
        category_id=fixture.categories["Продукты"],
        beneficiary_id=None,
        occurred_date=DAY,
        timezone=TZ,
        account_id=fixture.accounts["Карта"],
    )
    receivable = (await session.execute(select(Receivable))).scalar_one()
    settlement = None
    if collect:
        settlement = await settle_receivable(
            session,
            uow,
            actor=fixture.actor,
            receivable_id=receivable.id,
            amount=rub(1500),
            occurred_date=DAY,
            timezone=TZ,
            account_id=fixture.accounts["Карта"],
        )
    return purchase, receivable, settlement


async def obligation(session, uow, fixture):
    await create_schedule(
        session,
        uow,
        actor=fixture.actor,
        name="Регулярный платёж",
        direction="payment",
        rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=DAY),
        currency="RUB",
        expected=rub(1000),
        category_id=fixture.categories["Продукты"],
    )
    occurrence = (
        await materialize_occurrences(session, workspace_id=fixture.workspace.id, until_date=DAY)
    )[0]
    payment = await post_transaction(
        session,
        uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1000), category="Продукты", account="Карта"),
        origin="form",
    )
    await settle_occurrence(
        session,
        uow,
        actor=fixture.actor,
        occurrence_id=occurrence.id,
        transaction_id=payment.transaction_id,
        effect_id=payment.effect_id,
        amount=rub(1000),
    )
    return occurrence, payment


async def test_restored_receivable_settlement_reapplies_debt_coverage(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        _, receivable, settlement = await mixed(session, uow, fixture)
    async with command(test_settings, fixture) as (session, uow):
        voided = await void_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=settlement.transaction_id,
            expected_version=settlement.entity_version,
        )
    async with command(test_settings, fixture) as (session, uow):
        await restore_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=settlement.transaction_id,
            expected_version=voided.entity_version,
        )
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        balance = await account_balance(
            session, workspace_id=fixture.workspace.id, account_id=fixture.accounts["Карта"]
        )
        assert (row.outstanding_minor, row.status, balance) == (0, "settled", -150000)


async def test_revised_receivable_settlement_recomputes_debt(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        _, receivable, settlement = await mixed(session, uow, fixture)
    async with command(test_settings, fixture) as (session, uow):
        _, _, spec = await load_current_spec(
            session, workspace_id=fixture.workspace.id, transaction_id=settlement.transaction_id
        )
        amended = replace(
            spec,
            amount=rub(1000),
            allocations=(replace(spec.allocations[0], amount=rub(1000)),),
            cash_legs=(replace(spec.cash_legs[0], signed=rub(1000)),),
        )
        await revise_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=settlement.transaction_id,
            new_spec=amended,
            expected_version=settlement.entity_version,
        )
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        balance = await account_balance(
            session, workspace_id=fixture.workspace.id, account_id=fixture.accounts["Карта"]
        )
        assert (row.outstanding_minor, row.status, balance) == (50000, "open", -200000)


async def test_restored_scheduled_payment_reapplies_obligation_coverage(
    owner_session, test_settings
):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        occurrence, payment = await obligation(session, uow, fixture)
    async with command(test_settings, fixture) as (session, uow):
        voided = await void_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=payment.transaction_id,
            expected_version=payment.entity_version,
        )
    async with command(test_settings, fixture) as (session, uow):
        await restore_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=payment.transaction_id,
            expected_version=voided.entity_version,
        )
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Occurrence, occurrence.id)
        assert (row.state, row.settled_minor) == ("settled", 100000)


async def test_revised_scheduled_payment_recomputes_obligation_coverage(
    owner_session, test_settings
):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        occurrence, payment = await obligation(session, uow, fixture)
    async with command(test_settings, fixture) as (session, uow):
        _, _, spec = await load_current_spec(
            session, workspace_id=fixture.workspace.id, transaction_id=payment.transaction_id
        )
        amended = replace(
            spec,
            amount=rub(500),
            allocations=(replace(spec.allocations[0], amount=rub(500)),),
            cash_legs=(replace(spec.cash_legs[0], signed=rub(-500)),),
        )
        await revise_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=payment.transaction_id,
            new_spec=amended,
            expected_version=payment.entity_version,
        )
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Occurrence, occurrence.id)
        assert (row.state, row.settled_minor) == ("partially_settled", 50000)


async def test_void_uncollected_shared_purchase_removes_open_debt(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, receivable, _ = await mixed(session, uow, fixture, collect=False)
    async with command(test_settings, fixture) as (session, uow):
        await void_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=purchase.transaction_id,
            expected_version=purchase.entity_version,
        )
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        assert row.outstanding_minor == 0, (row.outstanding_minor, row.status)


async def test_cannot_shrink_collected_receivable_origin(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, _, _ = await mixed(session, uow, fixture)
    with pytest.raises(ConflictError):
        async with command(test_settings, fixture) as (session, uow):
            _, _, spec = await load_current_spec(
                session, workspace_id=fixture.workspace.id, transaction_id=purchase.transaction_id
            )
            amended = replace(
                spec,
                amount=rub(2000),
                allocations=(spec.allocations[0], replace(spec.allocations[1], amount=rub(500))),
                cash_legs=(replace(spec.cash_legs[0], signed=rub(-2000)),),
            )
            await revise_transaction(
                session,
                uow,
                actor=fixture.actor,
                transaction_id=purchase.transaction_id,
                new_spec=amended,
                expected_version=purchase.entity_version,
            )


async def test_restore_marks_accepted_reconciliation_stale(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        payment = await post_transaction(
            session,
            uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(1000), category="Продукты", account="Карта"),
            origin="form",
        )
        voided = await void_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=payment.transaction_id,
            expected_version=payment.entity_version,
        )
        rec = await record_reconciliation(
            session,
            uow,
            actor=fixture.actor,
            account_id=fixture.accounts["Карта"],
            cutoff_date=DAY,
            observed=rub(0),
        )
        await accept_reconciliation(
            session, uow, actor=fixture.actor, reconciliation_id=rec.reconciliation_id, adjust=False
        )
    async with command(test_settings, fixture) as (session, uow):
        await restore_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=payment.transaction_id,
            expected_version=voided.entity_version,
        )
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Reconciliation, rec.reconciliation_id)
        balance = await account_balance(
            session, workspace_id=fixture.workspace.id, account_id=fixture.accounts["Карта"]
        )
        assert (row.is_stale, balance) == (True, -100000)


async def test_change_account_marks_old_and_new_reconciliations_stale(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        payment = await post_transaction(
            session,
            uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(1000), category="Продукты", account="Карта"),
            origin="form",
        )
        old = await record_reconciliation(
            session,
            uow,
            actor=fixture.actor,
            account_id=fixture.accounts["Карта"],
            cutoff_date=DAY,
            observed=rub(-1000),
        )
        new = await record_reconciliation(
            session,
            uow,
            actor=fixture.actor,
            account_id=fixture.accounts["Кошелёк"],
            cutoff_date=DAY,
            observed=rub(0),
        )
        await accept_reconciliation(
            session, uow, actor=fixture.actor, reconciliation_id=old.reconciliation_id, adjust=False
        )
        await accept_reconciliation(
            session, uow, actor=fixture.actor, reconciliation_id=new.reconciliation_id, adjust=False
        )
    async with command(test_settings, fixture) as (session, uow):
        _, _, spec = await load_current_spec(
            session, workspace_id=fixture.workspace.id, transaction_id=payment.transaction_id
        )
        amended = replace(
            spec, cash_legs=(replace(spec.cash_legs[0], account_id=fixture.accounts["Кошелёк"]),)
        )
        await revise_transaction(
            session,
            uow,
            actor=fixture.actor,
            transaction_id=payment.transaction_id,
            new_spec=amended,
            expected_version=payment.entity_version,
        )
    async with command(test_settings, fixture) as (session, _):
        old_row = await session.get(Reconciliation, old.reconciliation_id)
        new_row = await session.get(Reconciliation, new.reconciliation_id)
        assert (old_row.is_stale, new_row.is_stale) == (True, True)


async def test_backdated_post_marks_accepted_reconciliation_stale(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        rec = await record_reconciliation(
            session,
            uow,
            actor=fixture.actor,
            account_id=fixture.accounts["Карта"],
            cutoff_date=DAY,
            observed=rub(0),
        )
        await accept_reconciliation(
            session, uow, actor=fixture.actor, reconciliation_id=rec.reconciliation_id, adjust=False
        )
    async with command(test_settings, fixture) as (session, uow):
        await post_transaction(
            session,
            uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(1000), category="Продукты", account="Карта"),
            origin="form",
        )
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Reconciliation, rec.reconciliation_id)
        balance = await account_balance(
            session, workspace_id=fixture.workspace.id, account_id=fixture.accounts["Карта"]
        )
        assert (row.is_stale, balance) == (True, -100000)


async def test_weekly_review_does_not_forecast_incomplete_history(owner_session, test_settings):
    fixture = await build_fixture(
        owner_session, start=dt.date(2026, 9, 1), limits={"Продукты": 100000}
    )
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        await post_transaction(
            session,
            uow,
            actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(800), category="Продукты"),
            origin="form",
        )
    async with command(test_settings, fixture) as (session, _):
        review = await build_weekly_review(
            session, workspace=fixture.workspace, today=dt.date(2026, 9, 14)
        )
        assert review.risky == (), [(line.forecast_minor, line.basis) for line in review.risky]


async def test_period_with_existing_plan_still_closes_predecessor(owner_session, test_settings):
    fixture = await build_fixture(
        owner_session, start=dt.date(2026, 8, 10), limits={"Продукты": 100000}
    )
    await _add_template(owner_session, fixture, limits={"Продукты": 100000})
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        period = await period_for_date(session, workspace_id=fixture.workspace.id, day=DAY)
        await apply_plan_for_period(session, uow, workspace_id=fixture.workspace.id, period=period)
    job = LeasedJob(
        id=uuid.uuid4(),
        job_type="open_next_period",
        queue_class="calendar",
        workspace_id=fixture.workspace.id,
        subject_id=None,
        payload={"local_date": "2026-09-12"},
        payload_version=1,
        attempts=1,
        max_attempts=6,
        lease_token=uuid.uuid4(),
        lease_until=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        deadline_at=None,
        correlation_id="money-recheck",
        logical_key="money-recheck-open",
    )
    await handle_open_next_period(test_settings, job)
    await handle_open_next_period(test_settings, job)
    async with command(test_settings, fixture) as (session, _):
        previous = await session.get(BudgetPeriod, fixture.period.id)
        assert previous.state == "ended", previous.state
