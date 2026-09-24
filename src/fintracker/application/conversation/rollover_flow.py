"""Category selection, remainder preview and explicit confirmation."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.planning.periods import period_for_date
from fintracker.application.planning.plan import period_status
from fintracker.application.planning.rollover_actions import accept_rollover
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import NotFound
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.planning import BudgetPeriod, Rollover
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork


async def rollover_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    back = (Button("← Следующий план", callback("menu", "nextplan")),)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        if action == "accept" and len(rest) == 2 and rest[1].lstrip("-").isdigit():
            proposals = (
                await session.scalars(select(Rollover).where(Rollover.workspace_id == workspace_id))
            ).all()
            proposal = next((row for row in proposals if short(row.id) == rest[0]), None)
            if proposal is None:
                raise NotFound("Предложение переноса недоступно")
            await accept_rollover(
                session,
                uow,
                actor=actor,
                rollover_id=proposal.id,
                today=today,
                expected_amount_minor=int(rest[1]),
            )
            return [
                Reply(
                    text="✅ Перенос принят\n\nЛимит следующего периода обновлён. "
                    "Повторное нажатие не добавляет сумму ещё раз.",
                    buttons=(back,),
                )
            ]

        current = await period_for_date(session, workspace_id=workspace_id, day=today)
        periods = (
            await session.scalars(
                select(BudgetPeriod)
                .where(
                    BudgetPeriod.workspace_id == workspace_id,
                    BudgetPeriod.start_date <= today,
                )
                .order_by(BudgetPeriod.start_date.desc())
            )
        ).all()
        source = next(row for row in periods if row.id == current.id)
        if rest:
            selected_period = next((row for row in periods if short(row.id) == rest[0]), None)
            if selected_period is None:
                raise NotFound("Период недоступен")
            source = selected_period
        destination = await period_for_date(
            session, workspace_id=workspace_id, day=source.end_exclusive
        )
        status = await period_status(
            session,
            workspace_id=workspace_id,
            period_id=source.id,
            currency=workspace.currency,
            today=today,
        )
        lines = [line for line in status.lines if line.remaining_minor]
        if action == "pick" and len(rest) > 1:
            selected = next((line for line in lines if short(line.stable_line_id) == rest[1]), None)
            if selected is None:
                raise NotFound("Остаток категории изменился. Откройте перенос заново.")
            amount = selected.remaining_minor
            assert amount is not None
            text = (
                f"↪️ Проверьте перенос\n\n{selected.category_name}\n"
                f"Остаток: {Money(amount, workspace.currency).format()}\n"
                f"Из периода {source.start_date:%d.%m.%Y} — "
                f"{source.end_exclusive - dt.timedelta(days=1):%d.%m.%Y}\n"
                f"В период с {destination.start_date:%d.%m.%Y}\n\n"
                "Меняется только лимит, движение денег не создаётся."
            )
            if amount < 0:
                text += "\nОтрицательный остаток уменьшит лимит следующего периода."
            buttons: list[tuple[Button, ...]] = []
            if source.end_exclusive > today:
                text += (
                    f"\n\n⏳ Пока это предварительная сумма: период продолжается. "
                    f"Подтвердить перенос можно с {source.end_exclusive:%d.%m.%Y}."
                )
            else:
                row = await session.scalar(
                    select(Rollover).where(
                        Rollover.workspace_id == workspace_id,
                        Rollover.source_period_id == source.id,
                        Rollover.destination_period_id == destination.id,
                        Rollover.stable_line_id == selected.stable_line_id,
                    )
                )
                if row is not None and row.status == "accepted":
                    text += "\n\n✅ Этот остаток уже перенесён."
                else:
                    if row is None:
                        row = Rollover(
                            workspace_id=workspace_id,
                            source_period_id=source.id,
                            destination_period_id=destination.id,
                            stable_line_id=selected.stable_line_id,
                            amount_minor=amount,
                            mode="signed",
                            status="proposed",
                            basis_completeness=source.completeness,
                        )
                        session.add(row)
                    else:
                        row.amount_minor = amount
                        row.status = "proposed"
                        row.basis_completeness = source.completeness
                    await session.flush()
                    text += "\n\nПроверьте, что все расходы исходного периода внесены."
                    buttons.append(
                        (
                            Button(
                                "✅ Подтвердить перенос",
                                callback("roll", "accept", short(row.id), str(amount)),
                            ),
                        )
                    )
            buttons.append(
                (Button("← Выбрать категорию", callback("roll", "show", short(source.id))),)
            )
            return [Reply(text=text, buttons=tuple(buttons))]

        page = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0
        page = max(0, min(page, max(0, (len(lines) - 1) // 6)))
        buttons = [
            (
                Button(
                    f"{line.category_name} · "
                    f"{Money(line.remaining_minor or 0, workspace.currency).format()}",
                    callback("roll", "pick", short(source.id), short(line.stable_line_id)),
                ),
            )
            for line in lines[page * 6 : (page + 1) * 6]
        ]
        navigation = []
        if page:
            navigation.append(
                Button("← Назад", callback("roll", "show", short(source.id), str(page - 1)))
            )
        if (page + 1) * 6 < len(lines):
            navigation.append(
                Button("Далее →", callback("roll", "show", short(source.id), str(page + 1)))
            )
        if navigation:
            buttons.append(tuple(navigation))
        previous = next((row for row in periods if row.end_exclusive == source.start_date), None)
        if previous:
            buttons.append(
                (Button("📅 Предыдущий период", callback("roll", "show", short(previous.id))),)
            )
        buttons.append(back)
        return [
            Reply(
                text=(
                    f"↪️ Перенос остатков\n\nПериод {source.start_date:%d.%m.%Y} — "
                    f"{source.end_exclusive - dt.timedelta(days=1):%d.%m.%Y}\n\n"
                    + (
                        "Выберите категорию для просмотра суммы и подтверждения."
                        if lines
                        else "Нет ненулевых остатков по категориям с лимитами."
                    )
                ),
                buttons=tuple(buttons),
            )
        ]
