from pathlib import Path
exec(Path('.planning/audits/2026-09-14-recheck-5646887/money/test_money_recheck.py').read_text().split('async def test_')[0], globals())
from fintracker.application.ledger.operations import post_refund
from fintracker.application.commitments.goals import create_goal, allocate_to_goal
from fintracker.application.conversation.goals_flow import apply_goal_amount
from fintracker.db.models.commitments import Goal
from fintracker.db.models.platform import Job, OutboxEvent
from fintracker.domain.ledger.model import AllocationRole, TransactionType


async def test_restore_shared_origin_reopens_uncollected_debt(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, receivable, _ = await mixed(session, uow, fixture, collect=False)
    async with command(test_settings, fixture) as (session, uow):
        cancelled = await void_transaction(session, uow, actor=fixture.actor,
            transaction_id=purchase.transaction_id, expected_version=purchase.entity_version)
    async with command(test_settings, fixture) as (session, uow):
        await restore_transaction(session, uow, actor=fixture.actor,
            transaction_id=purchase.transaction_id, expected_version=cancelled.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        assert (row.outstanding_minor, row.status) == (150000, 'open'), (row.outstanding_minor, row.status)


async def test_remove_uncollected_share_is_valid_correction(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, receivable, _ = await mixed(session, uow, fixture, collect=False)
    async with command(test_settings, fixture) as (session, uow):
        _, _, spec = await load_current_spec(session, workspace_id=fixture.workspace.id,
            transaction_id=purchase.transaction_id)
        own = next(a for a in spec.allocations if a.role is AllocationRole.EXPENSE)
        amended = replace(spec, transaction_type=TransactionType.EXPENSE,
            allocations=(replace(own, amount=spec.amount),))
        amended.validate()
        await revise_transaction(session, uow, actor=fixture.actor,
            transaction_id=purchase.transaction_id, new_spec=amended,
            expected_version=purchase.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        assert row.outstanding_minor == 0


@pytest.mark.parametrize('change', ['note', 'void', 'refund_void'])
async def test_refunded_share_survives_settlement_lifecycle(owner_session, test_settings, change):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, receivable, _ = await mixed(session, uow, fixture, collect=False)
        refund = await post_refund(session, uow, actor=fixture.actor,
            source_transaction_id=purchase.transaction_id,
            parts={receivable.origin_stable_line_id: rub(500)},
            occurred_date=DAY, timezone=TZ, account_id=fixture.accounts['Карта'])
        settlement = await settle_receivable(session, uow, actor=fixture.actor,
            receivable_id=receivable.id, amount=rub(500), occurred_date=DAY,
            timezone=TZ, account_id=fixture.accounts['Карта'])
    async with command(test_settings, fixture) as (session, uow):
        if change == 'note':
            _, _, spec = await load_current_spec(session, workspace_id=fixture.workspace.id,
                transaction_id=settlement.transaction_id)
            await revise_transaction(session, uow, actor=fixture.actor,
                transaction_id=settlement.transaction_id, new_spec=replace(spec, note='Комментарий'),
                expected_version=settlement.entity_version)
            expected = 50000
        else:
            target = refund if change == 'refund_void' else settlement
            await void_transaction(session, uow, actor=fixture.actor,
                transaction_id=target.transaction_id, expected_version=target.entity_version)
            expected = 100000
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Receivable, receivable.id)
        assert row.outstanding_minor == expected, (change, row.outstanding_minor, expected)


async def test_note_edit_preserves_partial_occurrence_settlement(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        await create_schedule(session, uow, actor=fixture.actor, name='Платёж', direction='payment',
            rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=DAY), currency='RUB',
            expected=rub(1000), category_id=fixture.categories['Продукты'])
        occurrence = (await materialize_occurrences(session,
            workspace_id=fixture.workspace.id, until_date=DAY))[0]
        spec = expense_spec(fixture, amount=rub(1000), category='Продукты', account='Карта')
        payment = await post_transaction(session, uow, actor=fixture.actor, spec=spec, origin='form')
        await settle_occurrence(session, uow, actor=fixture.actor, occurrence_id=occurrence.id,
            transaction_id=payment.transaction_id, effect_id=payment.effect_id, amount=rub(500))
    async with command(test_settings, fixture) as (session, uow):
        _, _, spec = await load_current_spec(session, workspace_id=fixture.workspace.id,
            transaction_id=payment.transaction_id)
        await revise_transaction(session, uow, actor=fixture.actor,
            transaction_id=payment.transaction_id, new_spec=replace(spec, note='Комментарий'),
            expected_version=payment.entity_version)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Occurrence, occurrence.id)
        assert (row.settled_minor, row.state) == (50000, 'partially_settled'), (row.settled_minor, row.state)


@pytest.mark.parametrize('input_text', ['-100', '100 USD', '1.500'])
async def test_goal_amount_needs_unambiguous_positive_currency(owner_session, test_settings, input_text):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        goal = await create_goal(session, uow, actor=fixture.actor, name='Цель', currency='RUB',
            target=rub(10000))
        await allocate_to_goal(session, uow, actor=fixture.actor, goal_id=goal.id, amount=rub(5000))
    replies = await apply_goal_amount(test_settings, actor=fixture.actor, workspace=fixture.workspace,
        goal_id=goal.id, operation='use', text=input_text)
    async with command(test_settings, fixture) as (session, _):
        row = await session.get(Goal, goal.id)
        assert row.allocated_minor == 500000, (input_text, row.allocated_minor, [r.text for r in replies])


async def test_existing_plan_still_schedules_period_opening(owner_session, test_settings):
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10),
        limits={'Продукты': 100000})
    await _add_template(owner_session, fixture, limits={'Продукты': 100000})
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        period = await period_for_date(session, workspace_id=fixture.workspace.id, day=DAY)
        await apply_plan_for_period(session, uow, workspace_id=fixture.workspace.id, period=period)
    job = LeasedJob(id=uuid.uuid4(), job_type='open_next_period', queue_class='calendar',
        workspace_id=fixture.workspace.id, subject_id=None, payload={'local_date':'2026-09-12'},
        payload_version=1, attempts=1, max_attempts=6, lease_token=uuid.uuid4(),
        lease_until=dt.datetime.now(dt.UTC)+dt.timedelta(minutes=5), deadline_at=None,
        correlation_id='audit-money-597', logical_key='audit-money-597-open')
    await handle_open_next_period(test_settings, job)
    await handle_open_next_period(test_settings, job)
    async with command(test_settings, fixture) as (session, _):
        jobs = (await session.execute(select(Job).where(Job.workspace_id == fixture.workspace.id,
            Job.job_type == 'plan_review'))).scalars().all()
        events = (await session.execute(select(OutboxEvent).where(
            OutboxEvent.workspace_id == fixture.workspace.id,
            OutboxEvent.event_type == 'BudgetPeriodOpened',
            OutboxEvent.aggregate_id == period.id))).scalars().all()
        assert (len(jobs), len(events)) == (1, 1), (len(jobs), len(events))
