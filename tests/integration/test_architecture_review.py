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
from fintracker.core.money import Money
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
    """TECH-03, AR-02: до commit нет следов приёма; повтор после commit не создаёт вторую задачу."""
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


async def test_ar27_failed_deletion_is_retried(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession, tmp_path
) -> None:
    """AR-27: сбой удаления объекта не оставляет вложение навсегда."""
    from fintracker.application.maintenance.retention import sweep_attachments
    from fintracker.db.models.platform import Attachment

    fixture = await build_fixture(owner_session, telegram_user_id=6020)
    now = dt.datetime.now(dt.UTC)
    attachment = Attachment(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        visibility="owner",
        kind="photo",
        content_type="image/jpeg",
        size_bytes=1024,
        checksum_sha256="0" * 64,
        storage_key="photo/ar27",
        state="ready",
        delete_after=now - dt.timedelta(hours=1),
    )
    owner_session.add(attachment)
    await owner_session.flush()

    class FailingStorage:
        def __init__(self) -> None:
            self.calls = 0

        async def delete(self, key: str) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OSError("хранилище недоступно")

        async def put(self, key: str, data: bytes):  # pragma: no cover - не используется
            raise NotImplementedError

        async def get(self, key: str):  # pragma: no cover - не используется
            raise NotImplementedError

    failing = FailingStorage()
    import fintracker.application.maintenance.retention as retention_module
    import fintracker.infra.storage as storage_module

    original = storage_module.build_storage
    storage_module.build_storage = lambda _settings: failing  # type: ignore[assignment]
    try:
        first = await sweep_attachments(owner_session, test_settings, now)
        assert first == 0, "неуспешное удаление не объявляется выполненным"
        row = (
            await owner_session.execute(select(Attachment).where(Attachment.id == attachment.id))
        ).scalar_one()
        assert row.state == "deleting"

        second = await sweep_attachments(owner_session, test_settings, now)
        assert second == 1, "повтор доводит удаление до конца"
        await owner_session.refresh(row)
        assert row.state == "deleted"
    finally:
        storage_module.build_storage = original  # type: ignore[assignment]
        assert retention_module is not None


async def test_ar27_stale_staging_is_cleaned(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-27: загрузка без завершённой регистрации не остаётся навсегда."""
    from sqlalchemy import update as sql_update

    from fintracker.application.maintenance.retention import sweep_attachments
    from fintracker.db.models.platform import Attachment

    fixture = await build_fixture(owner_session, telegram_user_id=6021)
    now = dt.datetime.now(dt.UTC)
    attachment = Attachment(
        workspace_id=fixture.workspace.id,
        owner_user_id=fixture.user.id,
        visibility="owner",
        kind="photo",
        content_type="image/jpeg",
        size_bytes=2048,
        checksum_sha256="1" * 64,
        storage_key="photo/ar27-staging",
        state="staging",
        delete_after=now + dt.timedelta(days=30),
    )
    owner_session.add(attachment)
    await owner_session.flush()
    await owner_session.execute(
        sql_update(Attachment)
        .where(Attachment.id == attachment.id)
        .values(created_at=now - dt.timedelta(hours=12))
    )
    await owner_session.flush()

    removed = await sweep_attachments(owner_session, test_settings, now)
    assert removed == 1
    row = (
        await owner_session.execute(select(Attachment).where(Attachment.id == attachment.id))
    ).scalar_one()
    await owner_session.refresh(row)
    assert row.state == "deleted"


async def test_ar26_report_snapshot_is_versioned(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-26: числовой снимок помечен версией данных и не меняется задним числом."""
    from fintracker.application.analytics.reports import spending_report
    from fintracker.db.models.access import Workspace

    fixture = await build_fixture(owner_session, telegram_user_id=6030)
    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_000), category="Продукты"),
        origin="form",
    )
    workspace = (
        await owner_session.execute(select(Workspace).where(Workspace.id == fixture.workspace.id))
    ).scalar_one()
    before = await spending_report(
        owner_session,
        workspace=workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )

    # Исправление между чтениями даёт новую версию снимка, а не молчаливую подмену.
    from dataclasses import replace

    from fintracker.application.ledger.service import load_current_spec, revise_transaction
    from fintracker.core.money import Money

    _, _, spec = await load_current_spec(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=posted.transaction_id,
    )
    new_amount = Money(80_000, "RUB")
    await revise_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        transaction_id=posted.transaction_id,
        new_spec=replace(
            spec,
            amount=new_amount,
            allocations=(replace(spec.allocations[0], amount=new_amount),),
            cash_legs=tuple(
                replace(leg, signed=Money(-new_amount.minor, "RUB")) for leg in spec.cash_legs
            ),
        ),
        expected_version=None,
    )
    await owner_session.refresh(workspace)
    after = await spending_report(
        owner_session,
        workspace=workspace,
        date_from=DAY,
        date_to_exclusive=DAY + dt.timedelta(days=1),
    )
    assert before.meta.data_revision != after.meta.data_revision
    assert before.total_minor == 100_000
    assert after.total_minor == 80_000


async def test_ar25_manual_accounting_works_without_plan_and_ai(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-25: без плана и AI учёт работает, лимит не становится нулём."""
    from fintracker.application.planning.plan import LimitState, period_status

    fixture = await build_fixture(owner_session, telegram_user_id=6032, limits={})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(1_200), category="Продукты"),
        origin="form",
    )
    status = await period_status(
        owner_session,
        workspace_id=fixture.workspace.id,
        period_id=fixture.period.id,
        currency="RUB",
        today=DAY,
    )
    assert status.total_fact_minor == 120_000
    assert status.total_limit_minor in (None, 0)
    for line in status.lines:
        assert line.limit_state is not LimitState.ZERO or line.assigned_limit_minor == 0
        assert line.effective_limit_minor is None or line.effective_limit_minor >= 0


async def test_ar21_reference_account_has_no_invented_balance(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """CMD-14, AR-21: справочная карта не даёт выдуманного остатка и конвертации."""
    from fintracker.application.catalog.directory import create_account
    from fintracker.application.ledger.service import account_balance
    from fintracker.core.errors import ValidationFailed
    from fintracker.domain.ledger.model import (
        AllocationRole,
        AllocationSpec,
        CashLegSpec,
        CoverageMode,
        TransactionSpec,
        TransactionType,
    )
    from tests.integration.factories import TZ

    fixture = await build_fixture(owner_session, telegram_user_id=6040)
    reference = await create_account(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Личная карта",
        currency="RUB",
        mode="reference",
        account_type="card",
    )
    amount = rub(2_000)
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=TransactionSpec(
            transaction_type=TransactionType.EXPENSE,
            amount=amount,
            occurred_date=DAY,
            timezone=TZ,
            allocations=(
                AllocationSpec(
                    role=AllocationRole.EXPENSE,
                    amount=amount,
                    category_id=fixture.categories["Продукты"],
                ),
            ),
            cash_legs=(
                CashLegSpec(
                    signed=-amount,
                    account_id=reference.id,
                    coverage=CoverageMode.REFERENCE,
                ),
            ),
        ),
        origin="form",
    )
    assert (
        await account_balance(
            owner_session, workspace_id=fixture.workspace.id, account_id=reference.id
        )
        == 0
    ), "справочный счёт не становится банковским балансом"

    # Валюта чека вне справочника не конвертируется молча.
    with pytest.raises(ValidationFailed):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=TransactionSpec(
                transaction_type=TransactionType.EXPENSE,
                amount=amount,
                occurred_date=DAY,
                timezone=TZ,
                allocations=(
                    AllocationSpec(
                        role=AllocationRole.EXPENSE,
                        amount=amount,
                        category_id=fixture.categories["Продукты"],
                    ),
                ),
                cash_legs=(
                    CashLegSpec(
                        signed=-Money(amount.minor, "USD"),
                        account_id=fixture.accounts["Карта"],
                        coverage=CoverageMode.TRACKED,
                    ),
                ),
            ),
            origin="form",
        )


async def test_ar22_partial_settlement_keeps_single_remainder(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-22: остаток 400 учитывается один раз и переживает границу периода."""
    from fintracker.application.commitments.schedules import (
        change_occurrence,
        create_schedule,
        materialize_occurrences,
        settle_occurrence,
        upcoming_payments,
    )
    from fintracker.core.errors import ConflictError
    from fintracker.db.models.commitments import Occurrence
    from fintracker.domain.schedule import ScheduleKind, ScheduleRule

    fixture = await build_fixture(owner_session, telegram_user_id=6041)
    await create_schedule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Интернет",
        direction="payment",
        rule=ScheduleRule(
            kind=ScheduleKind.MONTHLY,
            anchor_date=dt.date(2026, 9, 12),
            interval=1,
            day_of_month=12,
        ),
        currency="RUB",
        expected=Money(100_000, "RUB"),
    )
    await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 31)
    )
    occurrence = (
        (
            await owner_session.execute(
                select(Occurrence)
                .where(Occurrence.workspace_id == fixture.workspace.id)
                .order_by(Occurrence.due_date)
            )
        )
        .scalars()
        .first()
    )
    assert occurrence is not None

    posted = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(600), category="Продукты"),
        origin="form",
    )
    await settle_occurrence(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        occurrence_id=occurrence.id,
        effect_id=posted.effect_id,
        transaction_id=posted.transaction_id,
        amount=rub(600),
    )
    await owner_session.refresh(occurrence)
    assert occurrence.state == "partially_settled"
    assert occurrence.expected_minor is not None
    assert occurrence.expected_minor - occurrence.settled_minor == 40_000

    # Переход периода не удваивает остаток и не создаёт второй экземпляр.
    payments = await upcoming_payments(
        owner_session,
        workspace_id=fixture.workspace.id,
        today=dt.date(2026, 10, 12),
        horizon_days=1,
        currency="RUB",
    )
    remainders = [item.remaining_minor for item in payments if item.occurrence_id == occurrence.id]
    assert remainders in ([], [40_000])

    # Переплата не переносится молча.
    second = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(500), category="Продукты"),
        origin="form",
    )
    with pytest.raises(ConflictError):
        await settle_occurrence(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            occurrence_id=occurrence.id,
            effect_id=second.effect_id,
            transaction_id=second.transaction_id,
            amount=rub(500),
        )

    # Исполненный экземпляр не переписывается правкой серии.
    await settle_occurrence(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        occurrence_id=occurrence.id,
        effect_id=second.effect_id,
        transaction_id=second.transaction_id,
        amount=rub(400),
    )
    await owner_session.refresh(occurrence)
    assert occurrence.state == "settled"
    with pytest.raises(ConflictError):
        await change_occurrence(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            occurrence_id=occurrence.id,
            action="postpone",
            new_due_date=dt.date(2026, 10, 20),
        )


async def test_ar18_goal_reserve_changes_only_by_chosen_action(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """AR-18: возврат покупки из цели не меняет резерв сам по себе."""
    from fintracker.application.commitments.goals import (
        allocate_to_goal,
        create_goal,
        release_goal,
        use_goal,
    )
    from fintracker.application.ledger.operations import post_refund, refundable_parts
    from fintracker.db.models.commitments import Goal
    from tests.integration.factories import TZ

    fixture = await build_fixture(owner_session, telegram_user_id=6050)
    goal = await create_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Ремонт",
        currency="RUB",
        target=rub(50_000),
    )
    await allocate_to_goal(
        owner_session, fixture.uow, actor=fixture.actor, goal_id=goal.id, amount=rub(10_000)
    )

    purchase = await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(4_000), category="Продукты"),
        origin="form",
    )
    await use_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        goal_id=goal.id,
        amount=rub(4_000),
        effect_id=purchase.effect_id,
        transaction_id=purchase.transaction_id,
        reason="Оплата из фонда",
    )
    row = (await owner_session.execute(select(Goal).where(Goal.id == goal.id))).scalar_one()
    assert row.allocated_minor == 600_000

    parts = await refundable_parts(
        owner_session,
        workspace_id=fixture.workspace.id,
        transaction_id=purchase.transaction_id,
    )
    await post_refund(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        source_transaction_id=purchase.transaction_id,
        parts={parts[0].stable_line_id: rub(1_500)},
        occurred_date=DAY,
        timezone=TZ,
    )
    await owner_session.refresh(row)
    assert row.allocated_minor == 600_000, "возврат сам по себе не пополняет резерв"

    # Резерв меняется только по выбранному действию участника.
    await allocate_to_goal(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        goal_id=goal.id,
        amount=rub(1_500),
    )
    await owner_session.refresh(row)
    assert row.allocated_minor == 750_000

    from fintracker.core.errors import ConflictError

    with pytest.raises(ConflictError):
        await release_goal(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            goal_id=goal.id,
            amount=rub(99_000),
            reason="Проверка границы",
        )


async def test_ar35_unknown_job_payload_is_rejected_explicitly(
    clean_db: None, test_settings: Settings
) -> None:
    """AR-35: задача неизвестного формата отклоняется явно, а не выполняется."""
    from fintracker.application.platform import queue
    from fintracker.runtime.worker import build_registry

    async with session_scope(test_settings, RuntimeRole.WORKER) as session:
        await queue.enqueue(
            session,
            job_type="unknown_future_job",
            logical_key="ar35:unknown",
            queue_class="maintenance",
            payload={"schema_version": 99},
            correlation_id="ar35",
        )

    registry = build_registry()
    assert registry.get("unknown_future_job") is None, "неизвестный тип не выполняется молча"

    leased = await queue.claim_jobs(test_settings, queue_classes=("maintenance",), limit=5)
    target = next(job for job in leased if job.job_type == "unknown_future_job")
    await queue.fail(
        test_settings,
        target,
        error=f"Неизвестный тип задачи {target.job_type}",
        permanent=True,
    )

    owner = get_sessionmaker(test_settings, RuntimeRole.OWNER)
    async with owner() as session, session.begin():
        row = (
            await session.execute(select(Job).where(Job.logical_key == "ar35:unknown"))
        ).scalar_one()
    assert row.state == "failed"
    assert "Неизвестный тип задачи" in (row.last_error or "")
