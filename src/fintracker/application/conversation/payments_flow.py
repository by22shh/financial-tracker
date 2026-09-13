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
    if not rest:
        return [Reply(text="Кнопка устарела. Откройте раздел «Платежи».")]
    workspace_id = actor.require_workspace()
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
            await uow.lock_workspace(workspace_id)
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
