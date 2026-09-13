"""Напоминания о платежах и актуальность доставки (FR-45, FR-46, FR-53, A61–A63)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.commitments.reminders import (
    due_reminders,
    enqueue_payment_reminders,
)
from fintracker.application.commitments.schedules import create_schedule, materialize_occurrences
from fintracker.application.delivery.render import render_event
from fintracker.config import Settings
from fintracker.core.money import Money
from fintracker.db.models.commitments import Occurrence
from fintracker.db.models.platform import OutboxEvent
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.schedule import ScheduleKind, ScheduleRule
from tests.conftest import requires_pg
from tests.integration.factories import Fixture, build_fixture

pytestmark = [pytest.mark.pg, requires_pg]

DAY = dt.date(2026, 9, 12)


async def _monthly_rent(
    session: AsyncSession, fixture: Fixture, *, due_day: int = 15, reminder_days: int = 1
) -> uuid.UUID:
    item = await create_schedule(
        session,
        fixture.uow,
        actor=fixture.actor,
        name="Аренда",
        direction="payment",
        rule=ScheduleRule(
            kind=ScheduleKind.MONTHLY,
            anchor_date=dt.date(2026, 9, due_day),
            interval=1,
            day_of_month=due_day,
        ),
        currency="RUB",
        expected=Money(4_000_000, "RUB"),
        category_id=fixture.categories.get("Жильё"),
        reminder_days_before=reminder_days,
    )
    await materialize_occurrences(
        session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 31)
    )
    return item.id


async def test_reminder_appears_only_within_lead_time(owner_session: AsyncSession) -> None:
    """FR-45: напоминание появляется за заданное число дней до срока."""
    fixture = await build_fixture(owner_session)
    await _monthly_rent(owner_session, fixture, due_day=15, reminder_days=1)

    early = await due_reminders(
        owner_session, workspace_id=fixture.workspace.id, today=dt.date(2026, 9, 13)
    )
    assert early == []

    on_time = await due_reminders(
        owner_session, workspace_id=fixture.workspace.id, today=dt.date(2026, 9, 14)
    )
    assert len(on_time) == 1
    assert on_time[0][1] == "Аренда"


async def test_a61_settled_payment_is_not_reminded(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A61: платёж, отмеченный оплаченным к утру, не напоминается."""
    fixture = await build_fixture(owner_session, telegram_user_id=5301)
    await _monthly_rent(owner_session, fixture, due_day=15, reminder_days=3)
    occurrence = (
        (
            await owner_session.execute(
                select(Occurrence).where(Occurrence.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .first()
    )
    assert occurrence is not None
    event = await fixture.uow.emit(
        workspace_id=fixture.workspace.id,
        event_type="PaymentReminder",
        aggregate_type="occurrence",
        aggregate_id=occurrence.id,
        payload={"occurrence_id": str(occurrence.id), "local_date": DAY.isoformat()},
    )
    assert event is not None

    # К моменту отправки платёж уже закрыт.
    occurrence.state = "settled"
    occurrence.settled_minor = occurrence.expected_minor or 0
    await owner_session.flush()

    text, _ = await render_event(
        owner_session,
        workspace=fixture.workspace,
        event_type="PaymentReminder",
        payload={"occurrence_id": str(occurrence.id)},
        recipient_user_id=fixture.user.id,
    )
    assert text is None, "устаревшее напоминание не отправляется"


async def test_reminder_text_marks_expectation_not_expense(
    owner_session: AsyncSession,
) -> None:
    """FR-46: напоминание явно называет платёж ожидаемым, а не расходом."""
    fixture = await build_fixture(owner_session)
    await _monthly_rent(owner_session, fixture, due_day=15)
    occurrence = (
        (
            await owner_session.execute(
                select(Occurrence).where(Occurrence.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .first()
    )
    assert occurrence is not None

    text, buttons = await render_event(
        owner_session,
        workspace=fixture.workspace,
        event_type="PaymentReminder",
        payload={"occurrence_id": str(occurrence.id)},
        recipient_user_id=fixture.user.id,
    )
    assert text is not None
    assert "ожидаемый платёж, а не проведённый расход" in text
    assert buttons is not None
    labels = [button["text"] for row in buttons for button in row]
    assert labels == ["Оплачено", "Перенести", "Пропустить"]


async def test_reminders_are_not_duplicated_within_a_day(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """FR-53: повторный запуск в тот же день не создаёт второе напоминание."""
    fixture = await build_fixture(owner_session, telegram_user_id=5302)
    await _monthly_rent(owner_session, fixture, due_day=12, reminder_days=1)
    await owner_session.commit()

    first = await enqueue_payment_reminders(test_settings, workspace_id=fixture.workspace.id)
    second = await enqueue_payment_reminders(test_settings, workspace_id=fixture.workspace.id)
    assert first >= 1
    assert second == 0

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        events = (
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.event_type == "PaymentReminder")
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == first


async def test_a62_author_card_is_immediate_summary_waits(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A62: карточка автору приходит сразу, фоновое событие ждёт тихих часов."""
    import uuid as _uuid

    from fintracker.application.delivery.dispatch import expand_event
    from fintracker.application.ledger.service import post_transaction
    from fintracker.core.context import MembershipStatus, Role
    from fintracker.core.ids import new_generation
    from fintracker.db.models.access import Membership, NotificationPreference, User
    from fintracker.db.models.platform import NotificationDelivery
    from tests.integration.test_money_scenarios import expense_spec, rub

    fixture = await build_fixture(owner_session, telegram_user_id=5303)
    other = User(id=_uuid.uuid4(), telegram_user_id=5304)
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
    # Тихие часы получателя охватывают весь день: фоновое событие переносится.
    owner_session.add(
        NotificationPreference(
            user_id=other.id,
            workspace_id=fixture.workspace.id,
            settings={},
            quiet_hours_start=0,
            quiet_hours_end=23,
            timezone="Asia/Novosibirsk",
        )
    )
    await owner_session.flush()

    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(400), category="Продукты"),
        origin="telegram_text",
    )
    await owner_session.commit()

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "TransactionPosted")
            )
        ).scalar_one()
        await expand_event(session, test_settings, event)

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        rows = (await session.execute(select(NotificationDelivery))).scalars().all()
        by_user = {row.recipient_user_id: row for row in rows}
    now = dt.datetime.now(dt.UTC)
    assert by_user[fixture.user.id].delivery_class == "author_card"
    assert by_user[fixture.user.id].available_at <= now, "автору — сразу"
    assert by_user[other.id].delivery_class == "shared_change"


async def test_a63_import_sends_one_summary(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A63: импорт истории не рассылает уведомление по каждой прошлой строке."""
    from fintracker.application.delivery.dispatch import expand_event
    from fintracker.application.ledger.service import post_transaction
    from tests.integration.test_money_scenarios import expense_spec, rub

    fixture = await build_fixture(owner_session, telegram_user_id=5305)
    for index in range(3):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(
                fixture,
                amount=rub(100 + index),
                category="Продукты",
                occurred=dt.date(2026, 9, 11),
            ),
            origin="import",
        )
    await fixture.uow.emit(
        workspace_id=fixture.workspace.id,
        event_type="ImportCommitted",
        aggregate_type="import_batch",
        aggregate_id=fixture.workspace.id,
        payload={"batch_id": str(fixture.workspace.id), "rows": 3},
        actor_user_id=fixture.user.id,
    )
    await owner_session.commit()

    per_event: dict[str, int] = {}
    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        events = (await session.execute(select(OutboxEvent))).scalars().all()
        for event in events:
            plan = await expand_event(session, test_settings, event)
            per_event[event.event_type] = per_event.get(event.event_type, 0) + plan.created
    assert per_event.get("TransactionPosted", 0) == 0, "импортные строки не рассылаются"
    assert per_event.get("ImportCommitted") == 1, "об импорте сообщает одна сводка"

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "ImportCommitted")
            )
        ).scalar_one()
        text, _ = await render_event(
            session,
            workspace=fixture.workspace,
            event_type=event.event_type,
            payload=dict(event.payload),
            recipient_user_id=fixture.user.id,
        )
    assert text is not None
    assert "Импорт завершён" in text
    assert "не рассылаются" in text


async def test_a139_coinciding_proactive_events_are_merged(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A139: обзор и подготовка бюджета дают одну объединённую доставку."""
    from fintracker.application.delivery.dispatch import (
        expand_event,
        handle_deliver_notification,
    )
    from fintracker.application.platform import queue
    from fintracker.db.models.platform import NotificationDelivery
    from fintracker.infra.telegram.sender import RecordingSender, set_sender_override

    fixture = await build_fixture(owner_session, telegram_user_id=5401)
    period_end = fixture.period.end_exclusive - dt.timedelta(days=1)
    await fixture.uow.emit(
        workspace_id=fixture.workspace.id,
        event_type="PlanReviewDue",
        aggregate_type="budget_period",
        aggregate_id=fixture.period.id,
        payload={
            "period_id": str(fixture.period.id),
            "end_inclusive": period_end.isoformat(),
            "schema_version": 1,
        },
    )
    await fixture.uow.emit(
        workspace_id=fixture.workspace.id,
        event_type="BudgetPeriodOpened",
        aggregate_type="budget_period",
        aggregate_id=fixture.period.id,
        payload={
            "period_id": str(fixture.period.id),
            "start_date": fixture.period.start_date.isoformat(),
            "end_inclusive": period_end.isoformat(),
        },
    )
    await owner_session.commit()

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        events = (await session.execute(select(OutboxEvent))).scalars().all()
        for event in events:
            await expand_event(session, test_settings, event)
        review_event = next(event for event in events if event.event_type == "PlanReviewDue")
        # Тихие часы не должны задерживать обе доставки в этой проверке.
        await session.execute(
            NotificationDelivery.__table__.update().values(
                available_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
            )
        )

    sender = RecordingSender()
    set_sender_override(sender)
    try:
        async with session_scope(
            test_settings, RuntimeRole.WORKER, workspace_id=fixture.workspace.id
        ) as session:
            await queue.enqueue(
                session,
                job_type="deliver_notification",
                logical_key=f"deliver:{review_event.id}",
                queue_class="interactive",
                workspace_id=fixture.workspace.id,
                payload={"event_id": str(review_event.id)},
                correlation_id="a139",
            )
        jobs = await queue.claim_jobs(test_settings, queue_classes=("interactive",), limit=5)
        target = next(job for job in jobs if job.job_type == "deliver_notification")
        await handle_deliver_notification(test_settings, target)
    finally:
        set_sender_override(None)

    assert len(sender.sent) == 1, "одна объединённая доставка вместо нескольких"
    body = sender.sent[0]["text"]
    assert "Сводка по бюджету" in body

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        rows = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.delivery_class == "review"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.state for row in rows} == {"sent"}, "обе фоновые доставки закрыты одной отправкой"
    assert len({row.telegram_message_id for row in rows}) == 1


async def test_a161_quiet_hours_are_personal_for_each_recipient(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A161: автор получает ответ сразу, у остальных действуют личные режимы."""
    import uuid as _uuid

    from fintracker.application.delivery.dispatch import expand_event
    from fintracker.application.ledger.service import post_transaction
    from fintracker.core.context import MembershipStatus, Role
    from fintracker.core.ids import new_generation
    from fintracker.db.models.access import Membership, NotificationPreference, User
    from fintracker.db.models.platform import NotificationDelivery
    from tests.integration.test_money_scenarios import expense_spec, rub

    fixture = await build_fixture(owner_session, telegram_user_id=5501)
    quiet_user = User(id=_uuid.uuid4(), telegram_user_id=5502)
    open_user = User(id=_uuid.uuid4(), telegram_user_id=5503)
    owner_session.add_all([quiet_user, open_user])
    await owner_session.flush()
    for user in (quiet_user, open_user):
        owner_session.add(
            Membership(
                workspace_id=fixture.workspace.id,
                user_id=user.id,
                role=Role.MEMBER.value,
                status=MembershipStatus.ACTIVE.value,
                generation=new_generation(),
            )
        )
    owner_session.add(
        NotificationPreference(
            user_id=quiet_user.id,
            workspace_id=fixture.workspace.id,
            settings={},
            quiet_hours_start=0,
            quiet_hours_end=23,
            timezone="Asia/Novosibirsk",
        )
    )
    owner_session.add(
        NotificationPreference(
            user_id=open_user.id,
            workspace_id=fixture.workspace.id,
            settings={},
            quiet_hours_start=3,
            quiet_hours_end=4,
            timezone="Asia/Novosibirsk",
        )
    )
    await owner_session.flush()

    await fixture.uow.emit(
        workspace_id=fixture.workspace.id,
        event_type="ThresholdCrossed",
        aggregate_type="budget_line",
        aggregate_id=fixture.period.id,
        payload={"text": "Лимит статьи превышен", "threshold_type": "over"},
        actor_user_id=fixture.user.id,
    )
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(300), category="Продукты"),
        origin="telegram_text",
    )
    await owner_session.commit()

    async with session_scope(
        test_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        events = (await session.execute(select(OutboxEvent))).scalars().all()
        for event in events:
            await expand_event(session, test_settings, event)
        rows = (await session.execute(select(NotificationDelivery))).scalars().all()

    now = dt.datetime.now(dt.UTC)
    author_cards = [
        row
        for row in rows
        if row.recipient_user_id == fixture.user.id and row.delivery_class == "author_card"
    ]
    assert author_cards and all(row.available_at <= now for row in author_cards), "автору сразу"

    thresholds = {row.recipient_user_id: row for row in rows if row.delivery_class == "threshold"}
    assert thresholds[quiet_user.id].available_at > now, "тихие часы участника соблюдены"
    assert thresholds[open_user.id].available_at <= now, "у другого участника свой режим"
