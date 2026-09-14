"""Цели накоплений и фонды (FR-49–FR-51, CMD-21)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.conversation.views import empty_state, money
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.db.models.access import Workspace
from fintracker.db.models.commitments import Goal
from fintracker.db.session import RuntimeRole, session_scope


async def goals_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Раздельно: запланировано, выделено, подтверждено на счёте (FR-50)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        goals = (
            (
                await session.execute(
                    select(Goal)
                    .where(Goal.workspace_id == workspace_id, Goal.status != "closed")
                    .order_by(Goal.priority, Goal.name)
                )
            )
            .scalars()
            .all()
        )
    if not goals:
        return [
            Reply(
                text=empty_state("goals"),
                buttons=(
                    (
                        Button("Добавить цель", callback("goal", "new")),
                        Button("← Меню", callback("menu", "main")),
                    ),
                ),
            )
        ]
    lines = ["Цели накоплений:"]
    for goal in goals:
        allocated = money(goal.allocated_minor, goal.currency)
        if goal.target_minor:
            target = money(goal.target_minor, goal.currency)
            lines.append(f"• {goal.name}: выделено {allocated} из {target}")
        else:
            lines.append(f"• {goal.name}: выделено {allocated}")
        if goal.contribution_minor:
            frequency = {
                "per_period": "за период",
                "monthly": "в месяц",
                "weekly": "в неделю",
                "custom": "по своему календарю",
            }.get(goal.contribution_frequency, goal.contribution_frequency)
            lines.append(
                f"    План взноса: {money(goal.contribution_minor, goal.currency)} {frequency}"
            )
        if goal.due_date:
            lines.append(f"    Срок: {goal.due_date.isoformat()}")
    lines.append(
        "«Выделено» — резервирование внутри бюджета. Подтверждённый остаток на "
        "накопительном счёте показывается отдельно после сверки."
    )
    rows: list[tuple[Button, ...]] = [
        (Button(goal.name[:24], callback("goal", "open", short(goal.id))),) for goal in goals[:6]
    ]
    rows.append(
        (
            Button("Добавить цель", callback("goal", "new")),
            Button("← Меню", callback("menu", "main")),
        )
    )
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def create_goal_from_text(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, text: str
) -> list[Reply]:
    """Создать цель из ответа участника: «Название = сумма» (FR-48, G-14)."""
    from decimal import Decimal

    from fintracker.application.commitments.goals import create_goal
    from fintracker.core.errors import DomainError
    from fintracker.core.money import Money
    from fintracker.db.uow import UnitOfWork
    from fintracker.domain.parsing.amounts import parse_amounts

    parts = [item.strip() for item in text.split("=")]
    name = parts[0] if parts and parts[0] else None
    if not name:
        return [Reply(text="Не понял название цели. Отправьте «Название = сумма».")]
    amounts = parse_amounts(parts[1]) if len(parts) > 1 else parse_amounts(text)
    target = Money.from_decimal(Decimal(amounts[0].value), workspace.currency) if amounts else None

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            goal = await create_goal(
                session,
                uow,
                actor=actor,
                name=name[:120],
                currency=workspace.currency,
                target=target,
            )
        except DomainError as exc:
            return [Reply(text=exc.message)]
        goal_name = goal.name
    suffix = f" на {target.format()}" if target is not None else " без целевой суммы"
    return [
        Reply(
            text=f"Цель «{goal_name}»{suffix} создана. Резерв не списывает деньги со счёта.",
            buttons=((Button("Цели", callback("menu", "goals")),),),
        )
    ]


async def goal_detail(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, goal_id: uuid.UUID
) -> list[Reply]:
    """Карточка цели с действиями резерва (FR-49–FR-51)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        goal = (
            await session.execute(
                select(Goal).where(
                    Goal.workspace_id == workspace_id,
                    Goal.id == goal_id,
                    Goal.status != "closed",
                )
            )
        ).scalar_one_or_none()
    if goal is None:
        return [Reply(text="Цель не найдена. Откройте список целей заново.")]

    allocated = money(goal.allocated_minor, goal.currency)
    lines = [f"Цель «{goal.name}»", f"Выделено: {allocated}"]
    if goal.target_minor:
        target = money(goal.target_minor, goal.currency)
        remaining = money(max(0, goal.target_minor - goal.allocated_minor), goal.currency)
        lines.append(f"Цель: {target}")
        lines.append(f"Осталось: {remaining}")
    if goal.due_date:
        lines.append(f"Срок: {goal.due_date.isoformat()}")

    code = short(goal.id)
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("Выделить резерв", callback("goal", "allocate", code)),
                    Button("Использовать резерв", callback("goal", "use", code)),
                ),
                (
                    Button("Освободить резерв", callback("goal", "release", code)),
                    Button("← Цели", callback("menu", "goals")),
                ),
            ),
        )
    ]


async def apply_goal_amount(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    goal_id: uuid.UUID,
    operation: str,
    text: str,
) -> list[Reply]:
    """Применить сумму к резерву цели."""
    from fintracker.application.commitments.goals import allocate_to_goal, release_goal, use_goal
    from fintracker.core.errors import DomainError
    from fintracker.core.money import Money
    from fintracker.db.uow import UnitOfWork
    from fintracker.domain.parsing.amounts import parse_amounts

    amounts = parse_amounts(text)
    if not amounts:
        return [Reply(text="Не понял сумму. Отправьте число, например 5000.")]
    amount = Money.from_decimal(Decimal(amounts[0].value), workspace.currency)
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            if operation == "allocate":
                await allocate_to_goal(
                    session, uow, actor=actor, goal_id=goal_id, amount=amount, reason="telegram"
                )
                verb = "выделено"
            elif operation == "use":
                await use_goal(
                    session, uow, actor=actor, goal_id=goal_id, amount=amount, reason="telegram"
                )
                verb = "использовано"
            elif operation == "release":
                await release_goal(
                    session,
                    uow,
                    actor=actor,
                    goal_id=goal_id,
                    amount=amount,
                    reason="telegram",
                )
                verb = "освобождено"
            else:
                return [Reply(text="Действие недоступно.")]
        except DomainError as exc:
            return [Reply(text=exc.message)]
    return [
        Reply(
            text=f"По цели {verb}: {amount.format()}.",
            buttons=((Button("Открыть цель", callback("goal", "open", short(goal_id))),),),
        )
    ]


async def goal_action(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, action: str, rest: list[str]
) -> list[Reply]:
    """Кнопки раздела целей (FR-48, G-14)."""
    from fintracker.application.conversation.pending import set_pending

    workspace_id = actor.require_workspace()
    if action == "new":
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind="goal_new",
            payload={},
        )
        return [
            Reply(
                text=(
                    "Опишите цель одним сообщением: название и сумма.\n"
                    "Например: «Отпуск = 100000». Резерв не списывает деньги со счёта."
                )
            )
        ]

    async def _resolve_goal(prefix: str) -> uuid.UUID | None:
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            ids = (
                (
                    await session.execute(
                        select(Goal.id).where(
                            Goal.workspace_id == workspace_id,
                            Goal.status != "closed",
                        )
                    )
                )
                .scalars()
                .all()
            )
        matches = [goal_id for goal_id in ids if short(goal_id) == prefix]
        return matches[0] if len(matches) == 1 else None

    if action == "open" and rest:
        goal_id = await _resolve_goal(rest[0])
        if goal_id is None:
            return [Reply(text="Цель не найдена. Откройте список целей заново.")]
        return await goal_detail(settings, actor=actor, workspace=workspace, goal_id=goal_id)
    if action in {"allocate", "use", "release"} and rest:
        goal_id = await _resolve_goal(rest[0])
        if goal_id is None:
            return [Reply(text="Цель не найдена. Откройте список целей заново.")]
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind=f"goal_{action}",
            payload={"goal_id": str(goal_id)},
        )
        labels = {"allocate": "выделить", "use": "использовать", "release": "освободить"}
        return [Reply(text=f"Какую сумму {labels[action]}? Отправьте число, например 5000.")]
    return [Reply(text="Действие недоступно.")]
