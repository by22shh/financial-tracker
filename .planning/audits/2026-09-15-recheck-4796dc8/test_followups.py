"""Required behavior beyond the 23 frozen audit cases; no xfail or inverted checks."""

import datetime as dt
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from fintracker.application.commitments.goals import allocate_to_goal, create_goal
from fintracker.application.conversation.goals_flow import apply_goal_amount
from fintracker.application.conversation import pending as pending_service
from fintracker.application.conversation.service import handle
from fintracker.application.conversation.types import IncomingMessage, MessageKind
from fintracker.application.conversation.history_flow import JournalView, journal_view
from fintracker.application.delivery import render
from fintracker.application.delivery.dispatch import expand_event, handle_deliver_notification
from fintracker.application.identity.membership import delete_workspace, remove_member
from fintracker.application.identity.security_change import resume_or_quarantine, run_security_change
from fintracker.application.ledger.operations import post_mixed_payment, post_refund
from fintracker.application.ledger.service import (
    load_current_spec, post_transaction, restore_transaction, revise_transaction, void_transaction,
)
from fintracker.application.planning.rollover import handle_open_next_period
from fintracker.application.platform import queue
from fintracker.db.models.access import Membership, User, Workspace
from fintracker.db.models.commitments import Goal
from fintracker.db.models.ledger import Receivable
from fintracker.db.models.platform import Draft, HistoryQueryState, Job, NotificationDelivery, OutboxEvent, PendingAction
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.ledger.model import AllocationRole, TransactionType
from fintracker.infra.security_log import FilesystemSecurityLog, SecurityLog
from fintracker.infra.telegram.sender import RecordingSender, set_sender_override
from fintracker.runtime.worker import _run_with_lease, build_registry
from tests.acceptance.conftest import make_user
from tests.acceptance.helpers import create_budget
from tests.integration.factories import TZ, build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub
from tests.readiness.test_money_readiness import DAY, command
from tests.readiness.test_async_readiness import prepare_delivery


@pytest.fixture(autouse=True)
def recorded_sender():
    sender = RecordingSender()
    set_sender_override(sender)
    yield sender
    set_sender_override(None)


async def mixed(session, uow, fixture):
    purchase = await post_mixed_payment(
        session, uow, actor=fixture.actor, total=rub(3000), own_share=rub(1500),
        counterparty_label="Друг", counterparty_person_id=None,
        category_id=fixture.categories["Продукты"], beneficiary_id=None,
        occurred_date=DAY, timezone=TZ, account_id=fixture.accounts["Карта"],
    )
    receivable = (await session.scalars(select(Receivable))).one()
    return purchase, receivable


@pytest.mark.parametrize("action", ["note", "restore", "resize"])
async def test_refund_of_foreign_share_keeps_effect_after_revision(owner_session, test_settings, action):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, debt = await mixed(session, uow, fixture)
        refund = await post_refund(
            session, uow, actor=fixture.actor, source_transaction_id=purchase.transaction_id,
            parts={debt.origin_stable_line_id: rub(500)}, occurred_date=DAY, timezone=TZ,
            account_id=fixture.accounts["Карта"],
        )
    async with command(test_settings, fixture) as (session, uow):
        if action == "restore":
            voided = await void_transaction(session, uow, actor=fixture.actor,
                transaction_id=refund.transaction_id, expected_version=refund.entity_version)
    async with command(test_settings, fixture) as (session, uow):
        if action == "restore":
            await restore_transaction(session, uow, actor=fixture.actor,
                transaction_id=refund.transaction_id, expected_version=voided.entity_version)
        else:
            _, _, spec = await load_current_spec(session, workspace_id=fixture.workspace.id,
                transaction_id=refund.transaction_id)
            amended = replace(spec, note="Исправленная заметка")
            if action == "resize":
                amended = replace(amended, amount=rub(300),
                    allocations=(replace(spec.allocations[0], amount=rub(300)),),
                    cash_legs=(replace(spec.cash_legs[0], signed=rub(300)),))
            await revise_transaction(session, uow, actor=fixture.actor,
                transaction_id=refund.transaction_id, expected_version=refund.entity_version,
                new_spec=amended)
    async with command(test_settings, fixture) as (session, _):
        actual = await session.get(Receivable, debt.id)
        expected = 120000 if action == "resize" else 100000
        assert actual.outstanding_minor == expected, (action, actual.outstanding_minor, expected)


@pytest.mark.parametrize("action", ["note", "void"])
async def test_removed_share_does_not_block_later_correction(owner_session, test_settings, action):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        purchase, debt = await mixed(session, uow, fixture)
    async with command(test_settings, fixture) as (session, uow):
        _, _, original = await load_current_spec(session, workspace_id=fixture.workspace.id,
            transaction_id=purchase.transaction_id)
        own = next(a for a in original.allocations if a.role is AllocationRole.EXPENSE)
        spec = replace(original, transaction_type=TransactionType.EXPENSE,
            allocations=(replace(own, amount=original.amount),))
        edited = await revise_transaction(session, uow, actor=fixture.actor,
            transaction_id=purchase.transaction_id, new_spec=spec,
            expected_version=purchase.entity_version)
    async with command(test_settings, fixture) as (session, uow):
        if action == "note":
            await revise_transaction(session, uow, actor=fixture.actor,
                transaction_id=purchase.transaction_id, new_spec=replace(spec, note="Для дома"),
                expected_version=edited.entity_version)
        else:
            await void_transaction(session, uow, actor=fixture.actor,
                transaction_id=purchase.transaction_id, expected_version=edited.entity_version)


async def test_same_human_reason_does_not_deduplicate_distinct_goal_movements(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        goal = await create_goal(session, uow, actor=fixture.actor, name="Отпуск", currency="RUB")
        await allocate_to_goal(session, uow, actor=fixture.actor, goal_id=goal.id,
            amount=rub(100), reason="Еженедельный взнос")
        await allocate_to_goal(session, uow, actor=fixture.actor, goal_id=goal.id,
            amount=rub(200), reason="Еженедельный взнос")
        assert goal.allocated_minor == 30000, goal.allocated_minor


async def test_domain_rejection_preserves_pending_amount(bot, test_settings):
    user = make_user(test_settings, 99147901)
    await create_budget(user)
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    await user.send("Отпуск = 10000")
    await user.send("/goals")
    await user.press(user.button_data("Отпуск"))
    await user.press(user.button_data("Использовать резерв"))
    await user.send("100")
    assert "превышает" in user.text(), user.text()
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        pending = (await session.scalars(select(PendingAction))).all()
        assert len(pending) == 1 and pending[0].kind == "goal_use", user.text()


async def test_goal_retry_after_pending_cleanup_keeps_original_command(bot, test_settings, monkeypatch):
    user = make_user(test_settings, 99147903)
    await create_budget(user)
    await user.send("/goals")
    await user.press(user.button_data("Добавить цель"))
    await user.send("Отпуск = 10000")
    await user.send("/goals")
    await user.press(user.button_data("Отпуск"))
    await user.press(user.button_data("Выделить резерв"))
    message = IncomingMessage(telegram_user_id=user.telegram_user_id, chat_id=user.chat_id,
        kind=MessageKind.TEXT, text="5000", message_id=479603, correlation_id="same-input")
    original_clear = pending_service.clear_pending
    async def fail_after_cleanup(*args, **kwargs):
        await original_clear(*args, **kwargs)
        raise RuntimeError("crash after pending cleanup before answer is persisted")
    monkeypatch.setattr(pending_service, "clear_pending", fail_after_cleanup)
    with pytest.raises(RuntimeError, match="crash after"):
        await handle(test_settings, message)
    monkeypatch.setattr(pending_service, "clear_pending", original_clear)
    replies = await handle(test_settings, message)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        drafts = (await session.scalars(select(Draft).where(Draft.source_message_key==message.source_key))).all()
        assert drafts == [], ([r.text for r in replies], [(row.state, row.raw_text) for row in drafts])


async def test_signed_goal_amount_with_leading_words_is_not_reinterpreted(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    async with command(test_settings, fixture) as (session, uow):
        goal = await create_goal(session, uow, actor=fixture.actor, name="Отпуск", currency="RUB")
        await allocate_to_goal(session, uow, actor=fixture.actor, goal_id=goal.id, amount=rub(5000))
    replies = await apply_goal_amount(test_settings, actor=fixture.actor, workspace=fixture.workspace,
        goal_id=goal.id, operation="use", text="Сумма -100", idempotency_key="negative-input")
    async with command(test_settings, fixture) as (session, _):
        actual = await session.get(Goal, goal.id)
        assert actual.allocated_minor == 500000, (actual.allocated_minor, [r.text for r in replies])


async def test_expired_history_query_does_not_silently_remove_filter(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await post_transaction(owner_session, fixture.uow, actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(100), category="Продукты"), origin="form")
    await owner_session.commit()
    replies = await journal_view(test_settings, actor=fixture.actor, workspace=fixture.workspace,
        view=JournalView(flags="", sort="o", offset=0, category=""), note_query="Несуществующий отпуск")
    data = next(b.data for r in replies for row in r.buttons for b in row if b.text == "Фильтры")
    view = JournalView.parse(data.split(":")[2:])
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(HistoryQueryState).values(expires_at=dt.datetime.now(dt.UTC)-dt.timedelta(seconds=1)))
    page = await journal_view(test_settings, actor=fixture.actor, workspace=fixture.workspace, view=view)
    output = "\n".join(r.text for r in page)
    assert "из 1" not in output, output


async def test_purge_removes_full_history_query_text(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await owner_session.commit()
    await journal_view(test_settings, actor=fixture.actor, workspace=fixture.workspace,
        view=JournalView(flags="", sort="o", offset=0, category=""), note_query="Частный медицинский платёж 90000")
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        await session.execute(update(Workspace).where(Workspace.id == fixture.workspace.id).values(state="deleting"))
    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await session.execute(text("SELECT purge_workspace_data(:id)"), {"id": fixture.workspace.id})
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        remaining = (await session.scalars(select(HistoryQueryState.query))).all()
        assert remaining == [], remaining


async def test_period_retry_does_not_invalidate_unchanged_plan(owner_session, test_settings):
    fixture = await build_fixture(owner_session, start=dt.date(2026, 8, 10))
    await owner_session.commit()
    now = dt.datetime.now(dt.UTC)
    job = queue.LeasedJob(id=uuid.uuid4(), job_type="open_next_period", queue_class="calendar",
        workspace_id=fixture.workspace.id, subject_id=None, payload={}, payload_version=1,
        attempts=1, max_attempts=6, lease_token=uuid.uuid4(), lease_until=now+dt.timedelta(minutes=5),
        deadline_at=None, correlation_id="repeat-open", logical_key="repeat-open")
    await handle_open_next_period(test_settings, job)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        before = (await session.execute(select(Workspace.plan_revision, Workspace.calendar_revision).where(Workspace.id==fixture.workspace.id))).one()
    await handle_open_next_period(test_settings, job)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        after = (await session.execute(select(Workspace.plan_revision, Workspace.calendar_revision).where(Workspace.id==fixture.workspace.id))).one()
    assert before == after, (before, after)


async def member_fixture(owner_session, settings):
    fixture = await build_fixture(owner_session)
    member = User(id=uuid.uuid4(), telegram_user_id=99147902)
    owner_session.add(member)
    await owner_session.flush()
    generation = uuid.uuid4()
    owner_session.add(Membership(workspace_id=fixture.workspace.id, user_id=member.id,
        role="member", status="active", generation=generation))
    await owner_session.commit()
    return fixture, member, generation


async def test_terminal_deletion_notification_reaches_member(owner_session, test_settings, tmp_path, recorded_sender):
    settings = test_settings.model_copy(deep=True)
    settings.security_log.root = tmp_path / "security"
    fixture, member, generation = await member_fixture(owner_session, settings)
    await delete_workspace(settings, workspace_id=fixture.workspace.id, admin_user_id=fixture.user.id,
        confirmation_name=fixture.workspace.name, correlation_id="deletion-notification")
    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=fixture.workspace.id) as session:
        event = (await session.scalars(select(OutboxEvent).where(OutboxEvent.event_type=="BudgetDeletionRequested"))).one()
        await expand_event(session, settings, event)
        await queue.enqueue(session, job_type="deliver_notification", logical_key="terminal-notice",
            workspace_id=fixture.workspace.id, payload={"event_id": str(event.id)})
    job = (await queue.claim_jobs(settings, queue_classes=("interactive",)))[0]
    await _run_with_lease(settings, job, build_registry())
    assert any(item["chat_id"] == member.telegram_user_id for item in recorded_sender.sent), recorded_sender.sent


async def test_render_time_revocation_keeps_delivery_cancelled(owner_session, test_settings, tmp_path, monkeypatch, recorded_sender):
    settings = test_settings.model_copy(deep=True)
    settings.security_log.root = tmp_path / "security"
    fixture, member, generation = await member_fixture(owner_session, settings)
    async with command(settings, fixture) as (session, uow):
        await post_transaction(session, uow, actor=fixture.actor,
            spec=expense_spec(fixture, amount=rub(450), category="Продукты"), origin="form")
        await session.flush()
        event = (await session.scalars(select(OutboxEvent).where(OutboxEvent.event_type=="TransactionPosted"))).one()
        delivery = NotificationDelivery(event_id=event.id, workspace_id=fixture.workspace.id,
            recipient_user_id=member.id, membership_generation=generation, channel="telegram",
            delivery_class="shared_change", state="pending", available_at=dt.datetime.now(dt.UTC)-dt.timedelta(seconds=1))
        session.add(delivery)
        await session.flush()
        delivery_id = delivery.id
        await queue.enqueue(session, job_type="deliver_notification", logical_key="revoke-notice",
            workspace_id=fixture.workspace.id, payload={"event_id": str(event.id)})
    original = render.render_event
    async def revoke_after_render(*args, **kwargs):
        result = await original(*args, **kwargs)
        await remove_member(settings, workspace_id=fixture.workspace.id, admin_user_id=fixture.user.id,
            target_user_id=member.id, correlation_id="revoke-after-render")
        return result
    monkeypatch.setattr(render, "render_event", revoke_after_render)
    job = (await queue.claim_jobs(settings, queue_classes=("interactive",)))[0]
    await _run_with_lease(settings, job, build_registry())
    assert not recorded_sender.sent
    async with session_scope(settings, RuntimeRole.OWNER) as session:
        actual = await session.get(NotificationDelivery, delivery_id)
        assert actual.state == "cancelled", actual.state


async def test_journal_retry_does_not_hold_workspace_lock_during_storage_io(owner_session, test_settings, tmp_path, monkeypatch):
    settings = test_settings.model_copy(deep=True)
    settings.security_log.root = tmp_path / "security"
    fixture, member, generation = await member_fixture(owner_session, settings)
    storage = FilesystemSecurityLog(settings.security_log.root)
    journal = SecurityLog(storage)
    async def baseline(session, uow, workspace):
        return {}
    await run_security_change(settings, workspace_id=fixture.workspace.id, kind="workspace_create",
        initiated_by=fixture.user.id, apply=baseline, correlation_id="baseline", security_log=journal)
    original_last = journal.last_committed
    original_list = storage.list_keys
    probe = False
    locked_during_io = []
    async def detect_db_lock(prefix):
        if probe:
            async with session_scope(settings, RuntimeRole.OWNER) as session:
                try:
                    await session.execute(select(Workspace).where(Workspace.id==fixture.workspace.id).with_for_update(nowait=True))
                except DBAPIError:
                    locked_during_io.append(True)
        return await original_list(prefix)
    async def change_once(workspace_id):
        nonlocal probe
        old = await original_last(workspace_id)
        if not probe:
            await remove_member(settings, workspace_id=fixture.workspace.id,
                admin_user_id=fixture.user.id, target_user_id=member.id, correlation_id="concurrent-revoke")
            probe = True
        return old
    monkeypatch.setattr(storage, "list_keys", detect_db_lock)
    monkeypatch.setattr(journal, "last_committed", change_once)
    await resume_or_quarantine(settings, fixture.workspace.id, security_log=journal)
    assert locked_during_io == [], "Journal storage read holds the workspace row lock"
