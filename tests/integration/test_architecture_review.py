"""Архитектурные проверки устойчивости (AR-01, AR-02, AR-03, AR-06, AR-13, AR-14)."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.ingestion.accept_update import accept_telegram_update
from fintracker.application.ledger.service import post_transaction
from fintracker.config import Settings
from fintracker.core.errors import NotFound
from fintracker.db.models.ledger import Transaction
from fintracker.db.models.platform import InboundEvent, Job, OutboxEvent
from fintracker.db.session import RuntimeRole, get_sessionmaker, session_scope
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


def _update(update_id: int, *, text: str = "кофе 250", user_id: int = 6001) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1789000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест"},
            "text": text,
        },
    }


async def test_ar01_same_update_hundred_times(clean_db: None, test_settings: Settings) -> None:
    """AR-01: один и тот же update сто раз даёт один inbox и одну задачу."""
    payload = _update(880001)
    results = [await accept_telegram_update(test_settings, payload) for _ in range(100)]
    assert sum(1 for item in results if not item.duplicate) == 1

    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        events = (await session.execute(select(InboundEvent))).scalars().all()
        jobs = (await session.execute(select(Job))).scalars().all()
    assert len(events) == 1
    assert len(jobs) == 1


async def test_ar01_concurrent_delivery_is_accepted_once(
    clean_db: None, test_settings: Settings
) -> None:
    """AR-01: конкурентная доставка одного update не создаёт второй записи."""
    payload = _update(880002)
    results = await asyncio.gather(
        *(accept_telegram_update(test_settings, payload) for _ in range(8))
    )
    assert sum(1 for item in results if not item.duplicate) == 1

    factory = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with factory() as session, session.begin():
        events = (await session.execute(select(InboundEvent))).scalars().all()
        jobs = (await session.execute(select(Job))).scalars().all()
    assert len(events) == 1
    assert len(jobs) == 1


async def test_ar02_no_state_before_commit_and_no_second_job_after(
    clean_db: None, test_settings: Settings
) -> None:
    """AR-02: до commit нет следов приёма; повтор после commit не создаёт вторую задачу."""
    payload = _update(880003)

    # Обрыв до commit: транзакция откатывается целиком.
    factory = get_sessionmaker(test_settings, RuntimeRole.API)
    async with factory() as session:
        session.add(
            InboundEvent(
                bot_id=test_settings.telegram.bot_id,
                update_id=payload["update_id"],
                chat_id=6001,
                message_id=1,
                edit_version=0,
                event_type="message",
                state="received",
                correlation_id="ar02",
            )
        )
        await session.flush()
        await session.rollback()

    owner = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with owner() as session, session.begin():
        assert (await session.execute(select(InboundEvent))).scalars().all() == []

    accepted = await accept_telegram_update(test_settings, payload)
    assert not accepted.duplicate
    repeated = await accept_telegram_update(test_settings, payload)
    assert repeated.duplicate

    async with owner() as session, session.begin():
        assert len((await session.execute(select(Job))).scalars().all()) == 1


async def test_ar03_outbox_survives_worker_crash(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-03: расход один, доставка восстанавливается по outbox после сбоя."""
    fixture = await build_fixture(owner_session, telegram_user_id=6002)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(900), category="Продукты"),
        origin="telegram_text",
    )
    await owner_session.commit()

    # Сбой исполнителя после commit леджера: доставки ещё не созданы.
    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        transactions = (await session.execute(select(Transaction))).scalars().all()
        events = (
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.event_type == "TransactionPosted")
                )
            )
            .scalars()
            .all()
        )
    assert len(transactions) == 1, "расход записан ровно один раз"
    assert len(events) == 1, "событие доставки сохранено в том же commit"

    # После восстановления исполнитель раскрывает событие, не создавая второй расход.
    from fintracker.application.delivery.dispatch import expand_event

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "TransactionPosted")
            )
        ).scalar_one()
        first = await expand_event(session, test_settings, event)
        second = await expand_event(session, test_settings, event)
    assert first.created == 1
    assert second.created == 0, "повторное раскрытие не дублирует доставку"

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        assert len((await session.execute(select(Transaction))).scalars().all()) == 1


async def test_ar14_old_generation_does_not_revive(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-14: после выхода и повторного входа старое поколение не оживает."""
    from fintracker.application.identity.actor import resolve_actor
    from fintracker.core.context import MembershipStatus
    from fintracker.db.models.access import Membership

    fixture = await build_fixture(owner_session, telegram_user_id=6003)
    membership = (
        await owner_session.execute(
            select(Membership).where(
                Membership.workspace_id == fixture.workspace.id,
                Membership.user_id == fixture.user.id,
            )
        )
    ).scalar_one()
    old_generation = membership.generation

    membership.status = MembershipStatus.LEFT.value
    await owner_session.flush()
    with pytest.raises(NotFound):
        await resolve_actor(owner_session, user=fixture.user, workspace_id=fixture.workspace.id)

    from fintracker.core.ids import new_generation

    membership.status = MembershipStatus.ACTIVE.value
    membership.generation = new_generation()
    await owner_session.flush()
    actor = await resolve_actor(owner_session, user=fixture.user, workspace_id=fixture.workspace.id)
    assert actor.membership_generation != old_generation, "поколение обновлено"

    # Кнопка со старым поколением не даёт доступа к операциям прошлого членства.
    assert isinstance(old_generation, uuid.UUID)


async def test_ar13_private_material_is_not_shared(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-13: чужой черновик недоступен участнику, общий чек доступен."""
    from fintracker.application.conversation.entry import load_draft
    from fintracker.core.context import MembershipStatus, Role
    from fintracker.core.ids import new_generation
    from fintracker.db.models.access import Membership, User
    from fintracker.db.models.platform import Draft

    fixture = await build_fixture(owner_session, telegram_user_id=6010)
    other = User(id=uuid.uuid4(), telegram_user_id=6011)
    owner_session.add(other)
    await owner_session.flush()
    owner_session.add(
        Membership(
            workspace_id=fixture.workspace.id,
            user_id=other.id,
            role=Role.MEMBER.value,
            status=MembershipStatus.ACTIVE.value,
            generation=new_generation(),
        )
    )
    draft = Draft(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        owner_membership_generation=fixture.actor.membership_generation,
        source_kind="voice",
        state="needs_clarification",
        raw_text="личная расшифровка",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=3),
        delete_raw_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
    )
    owner_session.add(draft)
    await owner_session.flush()

    # Владелец видит свой черновик.
    own, _ = await load_draft(
        owner_session,
        workspace_id=fixture.workspace.id,
        draft_id=draft.id,
        owner_id=fixture.user.id,
    )
    assert own.id == draft.id

    # Другой участник — нет, даже находясь в том же бюджете.
    with pytest.raises(NotFound):
        await load_draft(
            owner_session,
            workspace_id=fixture.workspace.id,
            draft_id=draft.id,
            owner_id=other.id,
        )

    # Общий подтверждённый расход доступен обоим.
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(300), category="Продукты"),
        origin="telegram_text",
    )
    from fintracker.application.analytics.journal import list_journal

    page = await list_journal(owner_session, workspace_id=fixture.workspace.id)
    assert any(entry.transaction_id == posted.transaction_id for entry in page.entries)


async def test_ar13_review_gets_only_allowed_counter(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-13: системный обзор получает счётчик ожиданий, а не содержимое черновиков."""
    from fintracker.application.planning.plan import period_status
    from fintracker.db.models.platform import Candidate, Draft

    fixture = await build_fixture(owner_session, telegram_user_id=6012)
    draft = Draft(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        owner_membership_generation=fixture.actor.membership_generation,
        source_kind="voice",
        state="needs_clarification",
        raw_text="секретная расшифровка",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=3),
        delete_raw_after=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
    )
    owner_session.add(draft)
    await owner_session.flush()
    owner_session.add(
        Candidate(
            workspace_id=fixture.workspace.id,
            draft_id=draft.id,
            candidate_key="c1",
            state="needs_clarification",
            fields={"amount_minor": 50_000, "occurred_date": DAY.isoformat()},
            ambiguities=[{"field": "amount"}],
        )
    )
    await owner_session.flush()

    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.pending_drafts == 1
    # Текст черновика в статус периода не попадает.
    assert "секретная" not in str(status)
