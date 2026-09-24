"""TG-12: stale callbacks, mixed drafts and cancellation serialize correctly."""

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from fintracker.application.conversation import callbacks, sections
from fintracker.application.conversation.entry import (
    CandidateFields,
    ExtractionResult,
    create_draft_with_candidates,
    load_draft,
    post_draft,
)
from fintracker.application.conversation.keyboards import callback, short
from fintracker.application.conversation.pending import peek_pending
from fintracker.application.conversation.transaction_flow import (
    account_input,
    resolve_scoped_id,
    special_action,
)
from fintracker.application.ledger.service import post_transaction
from fintracker.core.errors import ConflictError, NotFound
from fintracker.db.models.catalog import Account
from fintracker.db.models.ledger import Transaction
from fintracker.db.models.platform import Candidate, Draft
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.parsing.intent import Intent
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]
DAY = dt.date(2026, 9, 12)


def field(kind, amount=10000, currency="RUB"):
    return CandidateFields(kind=kind, amount_minor=amount, currency=currency, occurred_date=DAY)


def button(replies, needle):
    for reply in replies:
        for row in reply.buttons:
            for item in row:
                assert len(item.data.encode()) <= 64
                if needle in item.text:
                    return item.data
    raise AssertionError((needle, replies))


async def press(settings, fixture, data):
    _, action, *rest = data.split(":")
    return await callbacks._draft_action(
        settings,
        actor=fixture.actor,
        workspace=fixture.workspace,
        action=action,
        rest=rest,
        user_id=fixture.user.id,
    )


async def draft_with(owner_session, settings, fixture, fields):
    draft, candidates = await create_draft_with_candidates(
        owner_session,
        settings=settings,
        actor=fixture.actor,
        source_kind="text",
        raw_text="Тест",
        extraction=ExtractionResult(intent=Intent.RECORD_TRANSACTION, candidates=fields),
    )
    await owner_session.commit()
    return draft, candidates


async def test_transfer_old_selection_cannot_erase_destination(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    draft, _ = await draft_with(owner_session, test_settings, fixture, [field("transfer")])
    replies = await special_action(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, draft_id=draft.id
    )
    old_from = button(replies, "Карта")
    replies = await press(test_settings, fixture, old_from)
    replies = await press(test_settings, fixture, button(replies, "Кошелёк"))
    confirm = button(replies, "Записать")
    with pytest.raises(ConflictError):
        await press(test_settings, fixture, old_from)
    await press(test_settings, fixture, confirm)
    await press(test_settings, fixture, confirm)
    await press(test_settings, fixture, callback("dr", "cancel", short(draft.id)))
    await press(test_settings, fixture, confirm)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        assert (await session.get(Draft, draft.id)).state == "posted"
        assert len((await session.scalars(select(Transaction))).all()) == 1


async def test_refund_double_click_stays_on_its_candidate_and_amount_edit_is_scoped(
    owner_session, test_settings
):
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(800), category="Продукты"),
        origin="form",
    )
    draft, candidates = await draft_with(
        owner_session, test_settings, fixture, [field("refund"), field("refund", 90000)]
    )
    replies = await special_action(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, draft_id=draft.id
    )
    first_source = button(replies, "800")
    replies = await press(test_settings, fixture, first_source)
    with pytest.raises(ConflictError):
        await press(test_settings, fixture, first_source)
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        second = await session.get(Candidate, candidates[1].id)
        assert second.fields["operation_details"] == {}
    replies = await press(test_settings, fixture, button(replies, "Записать"))
    replies = await press(test_settings, fixture, button(replies, "800"))
    assert "больше, чем осталось вернуть" in replies[0].text
    await press(test_settings, fixture, button(replies, "Изменить сумму"))
    pending = await peek_pending(
        test_settings, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    )
    assert pending.payload["candidate_id"] == str(candidates[1].id)
    await sections.apply_draft_edit(
        test_settings,
        actor=fixture.actor,
        workspace=fixture.workspace,
        draft_id=draft.id,
        candidate_id=uuid.UUID(pending.payload["candidate_id"]),
        expected_version=pending.payload["candidate_version"],
        text="200",
    )
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        first = await session.get(Candidate, candidates[0].id)
        second = await session.get(Candidate, candidates[1].id)
        assert first.fields["amount_minor"] == 10000
        assert second.fields["amount_minor"] == 20000
    replies = await sections.confirm_draft(
        test_settings,
        actor=fixture.actor,
        workspace=fixture.workspace,
        draft_id=draft.id,
        origin="telegram_text",
    )
    await press(test_settings, fixture, button(replies, "Записать"))
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        assert len((await session.scalars(select(Transaction))).all()) == 3


async def test_account_creation_uses_selected_transfer_currency(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    draft, candidates = await draft_with(
        owner_session,
        test_settings,
        fixture,
        [field("transfer"), field("transfer", currency="USD")],
    )
    replies = await special_action(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, draft_id=draft.id
    )
    replies = await press(test_settings, fixture, button(replies, "Карта"))
    replies = await press(test_settings, fixture, button(replies, "Кошелёк"))
    replies = await press(test_settings, fixture, button(replies, "Записать"))
    await press(test_settings, fixture, button(replies, "Новый счёт"))
    pending = await peek_pending(
        test_settings, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    )
    assert pending.payload["candidate_id"] == str(candidates[1].id)
    replies = await account_input(
        test_settings,
        actor=fixture.actor,
        workspace=fixture.workspace,
        pending=pending,
        text="Долларовая карта",
    )
    assert "С какого счёта" in replies[0].text
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        account = (
            await session.scalars(select(Account).where(Account.name == "Долларовая карта"))
        ).one()
        assert account.currency == "USD"


async def test_cancel_waits_for_posting_lock_and_keeps_posted_result(
    owner_session, test_settings, monkeypatch
):
    fixture = await build_fixture(owner_session)
    draft, _ = await draft_with(owner_session, test_settings, fixture, [field("expense")])
    resolved = asyncio.Event()
    original = callbacks._resolve_uuid

    async def resolve(*args, **kwargs):
        result = await original(*args, **kwargs)
        resolved.set()
        return result

    monkeypatch.setattr(callbacks, "_resolve_uuid", resolve)
    async with session_scope(
        test_settings, RuntimeRole.API, user_id=fixture.user.id, workspace_id=fixture.workspace.id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id="concurrent-post")
        await uow.lock_workspace(fixture.workspace.id, actor=fixture.actor)
        cancel = asyncio.create_task(
            press(test_settings, fixture, callback("dr", "cancel", short(draft.id)))
        )
        await asyncio.wait_for(resolved.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not cancel.done()
        current, candidates = await load_draft(
            session, workspace_id=fixture.workspace.id, draft_id=draft.id, owner_id=fixture.user.id
        )
        await post_draft(
            session,
            uow,
            actor=fixture.actor,
            draft=current,
            candidates=candidates,
            timezone=fixture.workspace.timezone,
            workspace_currency="RUB",
            origin="telegram_text",
        )
    replies = await asyncio.wait_for(cancel, timeout=5)
    assert "записан" in replies[0].text.lower()
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        assert (await session.get(Draft, draft.id)).state == "posted"


def test_short_scoped_ids_reject_collision_and_foreign_object():
    first = uuid.UUID("12345678-1111-1111-1111-111111111111")
    second = uuid.UUID("12345678-2222-2222-2222-222222222222")
    with pytest.raises(NotFound):
        resolve_scoped_id("12345678", [first, second])
    with pytest.raises(NotFound):
        resolve_scoped_id("87654321", [first])
    assert len(callback("dr", "source", "a" * 16, "b" * 8, str(2**63 - 1), "c" * 8)) == 64


async def test_refund_batch_capacity_failure_rolls_back_entire_batch(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(800), category="Продукты"),
        origin="form",
    )
    draft, _ = await draft_with(
        owner_session, test_settings, fixture, [field("refund", 50000), field("refund", 50000)]
    )
    replies = await special_action(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, draft_id=draft.id
    )
    replies = await press(test_settings, fixture, button(replies, "800"))
    replies = await press(test_settings, fixture, button(replies, "Записать"))
    replies = await press(test_settings, fixture, button(replies, "800"))
    with pytest.raises(ConflictError, match="превышает"):
        await press(test_settings, fixture, button(replies, "Записать"))
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        assert len((await session.scalars(select(Transaction))).all()) == 1
        saved = (
            await session.scalars(select(Candidate).where(Candidate.draft_id == draft.id))
        ).all()
        assert all(c.state == "ready" and c.posted_transaction_id is None for c in saved)


async def test_cancel_clears_only_its_pending_input_and_prevents_post(owner_session, test_settings):
    fixture = await build_fixture(owner_session)
    draft, _ = await draft_with(owner_session, test_settings, fixture, [field("transfer")])
    replies = await special_action(
        test_settings, actor=fixture.actor, workspace=fixture.workspace, draft_id=draft.id
    )
    await press(test_settings, fixture, button(replies, "Новый счёт"))
    assert (
        await peek_pending(
            test_settings, user_id=fixture.user.id, workspace_id=fixture.workspace.id
        )
        is not None
    )
    cancel = callback("dr", "cancel", short(draft.id))
    await press(test_settings, fixture, cancel)
    await press(test_settings, fixture, cancel)
    assert (
        await peek_pending(
            test_settings, user_id=fixture.user.id, workspace_id=fixture.workspace.id
        )
        is None
    )
    with pytest.raises(NotFound):
        await press(test_settings, fixture, callback("dr", "post", short(draft.id)))
    async with session_scope(test_settings, RuntimeRole.OWNER) as session:
        assert (await session.get(Draft, draft.id)).state == "cancelled"
        assert not (await session.scalars(select(Transaction))).all()
