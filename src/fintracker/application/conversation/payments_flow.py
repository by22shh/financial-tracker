"""Плановые платежи в диалоге (FR-45, FR-46).

Наступление даты создаёт ожидаемый платёж, а не расход: «Оплачено» ведёт к
обычной записи траты, связанной с этим экземпляром.
"""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.commitments.schedules import change_occurrence, upcoming_payments
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.conversation.views import money
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import DomainError, NotFound
from fintracker.db.models.access import Workspace
from fintracker.db.models.commitments import Occurrence
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

POSTPONE_DAYS = 3


async def payments_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Ближайшие и просроченные обязательства (FR-45)."""
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        payments = await upcoming_payments(
            session,
            workspace_id=workspace_id,
            today=today,
            horizon_days=45,
            currency=workspace.currency,
        )
    if not payments:
        from fintracker.application.conversation import views

        return [
            Reply(
                text=views.empty_state("payments"),
                buttons=((Button("← Меню", callback("menu", "more")),),),
            )
        ]
    lines = ["Плановые платежи:"]
    rows: list[tuple[Button, ...]] = []
    for item in payments[:6]:
        marker = "просрочен" if item.is_overdue else item.due_date.isoformat()
        lines.append(
            f"• {item.schedule_name} — {marker}, {money(item.remaining_minor, item.currency)}"
        )
        rows.append(
            (
                Button(
                    f"Оплачено: {item.schedule_name}"[:40],
                    callback("pay", "done", short(item.occurrence_id)),
                ),
            )
        )
    lines.append("Ожидаемый платёж не является расходом, пока не записан факт.")
    rows.append((Button("← Меню", callback("menu", "more")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def payment_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Кнопки напоминания: «Оплачено», «Перенести», «Пропустить» (FR-46)."""
    workspace_id = actor.require_workspace()
    if action == "new":
        from fintracker.application.conversation.pending import set_pending

        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind="payment_new",
            payload={},
        )
        return [
            Reply(
                text=(
                    "Опишите платёж одним сообщением: название, сумма и дата.\n"
                    "Например: «Интернет = 900 = 20.09»."
                )
            )
        ]
    if not rest:
        return [Reply(text="Кнопка устарела. Откройте раздел «Платежи».")]
    prefix = rest[0]
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            (
                await session.execute(
                    select(Occurrence).where(Occurrence.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        target: Occurrence | None = next((row for row in rows if short(row.id) == prefix), None)
        if target is None:
            raise NotFound("Ожидаемый платёж недоступен")
        occurrence_id: uuid.UUID = target.id
        expected = target.expected_minor
        settled = target.settled_minor
        due_date = target.due_date

        if action in {"move", "skip"}:
            uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
            await uow.lock_workspace(workspace_id, actor=actor)
            try:
                if action == "move":
                    await change_occurrence(
                        session,
                        uow,
                        actor=actor,
                        occurrence_id=occurrence_id,
                        action="postpone",
                        new_due_date=due_date + dt.timedelta(days=POSTPONE_DAYS),
                        reason="Перенос из напоминания",
                    )
                else:
                    await change_occurrence(
                        session,
                        uow,
                        actor=actor,
                        occurrence_id=occurrence_id,
                        action="skip",
                        reason="Пропуск из напоминания",
                    )
            except DomainError as exc:
                return [Reply(text=exc.message)]

    if action == "move":
        moved = due_date + dt.timedelta(days=POSTPONE_DAYS)
        return [
            Reply(
                text=(
                    f"Платёж перенесён на {moved.isoformat()}. "
                    "Расход не записан: перенос меняет только ожидание."
                ),
                buttons=((Button("Платежи", callback("menu", "payments")),),),
            )
        ]
    if action == "skip":
        return [
            Reply(
                text=("Платёж пропущен в этом цикле. Расписание сохранено, дохода это не создаёт."),
                buttons=((Button("Платежи", callback("menu", "payments")),),),
            )
        ]

    remaining = (expected or 0) - settled
    amount_hint = money(remaining, workspace.currency) if expected is not None else "сумму"
    if action == "paid":
        # Следующая подтверждённая трата закроет именно этот экземпляр (FR-46).
        from fintracker.application.conversation.pending import set_pending

        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind="occurrence_settle",
            payload={"occurrence_id": str(occurrence_id)},
        )
    return [
        Reply(
            text=(
                "Запишите факт оплаты: отправьте сообщение с суммой, например "
                f"«оплатил {amount_hint}».\n"
                "Ожидаемый платёж закроется после записи расхода, "
                "отдельной покупки при этом не появится."
            ),
            buttons=(
                (
                    Button("Ручной ввод", callback("menu", "add")),
                    Button("← Платежи", callback("menu", "payments")),
                ),
            ),
        )
    ]


async def create_payment_from_text(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, text: str
) -> list[Reply]:
    """Создать ожидаемый платёж из ответа участника (FR-45, G-15).

    Формат «Название = сумма = дата»: дата необязательна, сумма нужна для
    напоминания и плана.
    """
    from decimal import Decimal

    from fintracker.application.commitments.schedules import create_schedule
    from fintracker.core.money import Money
    from fintracker.domain.parsing.amounts import parse_amounts
    from fintracker.domain.parsing.dates import resolve_date_expression
    from fintracker.domain.schedule import ScheduleKind, ScheduleRule

    parts = [item.strip() for item in text.split("=")]
    name = parts[0] if parts and parts[0] else None
    if not name:
        return [Reply(text="Не понял название платежа. Отправьте «Название = сумма = дата».")]
    amounts = parse_amounts(parts[1]) if len(parts) > 1 else parse_amounts(text)
    if not amounts:
        return [Reply(text="Не понял сумму платежа. Отправьте «Название = сумма = дата».")]
    expected = Money.from_decimal(Decimal(amounts[0].value), workspace.currency)

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    anchor = today
    if len(parts) > 2 and parts[2]:
        parsed = resolve_date_expression(parts[2], reference=today)
        if parsed is not None:
            anchor = parsed.value
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            item = await create_schedule(
                session,
                uow,
                actor=actor,
                name=name[:120],
                direction="payment",
                rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=anchor),
                currency=workspace.currency,
                expected=expected,
            )
        except DomainError as exc:
            return [Reply(text=exc.message)]
        item_name = item.name
    return [
        Reply(
            text=(
                f"Платёж «{item_name}» на {expected.format()} создан, "
                f"ближайший срок {anchor.isoformat()}. Расход появится после записи оплаты."
            ),
            buttons=((Button("Платежи", callback("menu", "payments")),),),
        )
    ]
