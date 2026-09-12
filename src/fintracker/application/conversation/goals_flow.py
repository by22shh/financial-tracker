"""Цели накоплений и фонды (FR-49–FR-51, CMD-21)."""

from __future__ import annotations

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
