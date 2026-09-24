"""Цели накоплений и фонды (FR-49–FR-51, CMD-21)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.conversation.views import empty_state, format_date, money
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.db.models.access import Workspace
from fintracker.db.models.commitments import Goal
from fintracker.db.session import RuntimeRole, session_scope

GOALS_PAGE_SIZE = 6


async def goals_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, page: int = 0
) -> list[Reply]:
    """Показать одну согласованную страницу целей и кнопок (FR-50)."""
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
                        Button("➕ Добавить цель", callback("goal", "new")),
                        Button("← Меню", callback("menu", "main")),
                    ),
                ),
            )
        ]
    pages = max(1, (len(goals) + GOALS_PAGE_SIZE - 1) // GOALS_PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    visible = goals[page * GOALS_PAGE_SIZE : (page + 1) * GOALS_PAGE_SIZE]
    lines = ["🎯 Цели накоплений"]
    if pages > 1:
        lines.append(f"Страница {page + 1} из {pages}")
    lines.append("")
    for goal in visible:
        allocated = money(goal.allocated_minor, goal.currency)
        if goal.target_minor:
            target = money(goal.target_minor, goal.currency)
            percent = min(100, goal.allocated_minor * 100 // goal.target_minor)
            lines.append(
                f"{goal.name}\n{_progress_bar(percent)} {percent}% · {allocated} из {target}"
            )
        else:
            lines.append(f"{goal.name}\nОтложено: {allocated}")
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
            lines.append(f"    Срок: {format_date(goal.due_date, with_year=True)}")
        lines.append("")
    lines.append(
        "ℹ️ «Отложено» — деньги, которые вы решили не тратить в бюджете. На банковских "
        "счетах они никуда не переводятся."
    )
    rows: list[tuple[Button, ...]] = [
        (Button(goal.name[:24], callback("goal", "open", short(goal.id))),) for goal in visible
    ]
    navigation: list[Button] = []
    if page:
        navigation.append(Button("← Предыдущие", callback("goal", "page", str(page - 1))))
    if page + 1 < pages:
        navigation.append(Button("Следующие →", callback("goal", "page", str(page + 1))))
    if navigation:
        rows.append(tuple(navigation))
    rows.append(
        (
            Button("➕ Добавить цель", callback("goal", "new")),
            Button("← Меню", callback("menu", "main")),
        )
    )
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def create_goal_from_text(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    text: str,
    pending_payload: dict[str, Any] | None = None,
) -> list[Reply]:
    """Создать цель коротким диалогом; пакетный формат остаётся дополнительным."""
    from decimal import Decimal

    from fintracker.application.commitments.goals import create_goal
    from fintracker.application.conversation.pending import set_pending
    from fintracker.core.errors import DomainError
    from fintracker.core.money import Money
    from fintracker.db.uow import UnitOfWork
    from fintracker.domain.parsing.amounts import parse_amounts

    payload = pending_payload or {}
    name: str | None
    if payload.get("step") != "amount" and "=" not in text:
        name = text.strip()
        if not name or parse_amounts(name):
            return [
                Reply(
                    text="🎯 Как назвать цель?\n\nНапример: Отпуск или Подушка безопасности.",
                    retry_input=True,
                )
            ]
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=actor.require_workspace(),
            kind="goal_new",
            payload={"step": "amount", "name": name[:120]},
        )
        return [
            Reply(
                text=(
                    f"🎯 Цель: {name[:120]}\n\nКакая сумма нужна?\n"
                    f"Отправьте число в {workspace.currency}, например 100000."
                ),
                retry_input=True,
            )
        ]

    if payload.get("step") == "amount":
        name = str(payload.get("name") or "").strip()
        parts = [name, text.strip()]
    else:
        parts = [item.strip() for item in text.split("=")]
    name = parts[0] if parts and parts[0] else None
    if not name:
        return [
            Reply(
                text=(
                    "✍️ Уточните название цели\n\nОтправьте название и сумму в таком "
                    "формате:\nОтпуск = 100000"
                ),
                retry_input=True,
            )
        ]
    amounts = parse_amounts(parts[1]) if len(parts) > 1 else parse_amounts(text)
    if payload.get("step") == "amount" and not amounts:
        return [
            Reply(
                text=(
                    f"✍️ Укажите целевую сумму в {workspace.currency}\n\n"
                    "Отправьте число, например 100000. /cancel — отменить."
                ),
                retry_input=True,
            )
        ]
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
            return [Reply(text=f"⚠️ {exc.message}", retry_input=True)]
        goal_name = goal.name
        goal_id = goal.id
    suffix = f" на {target.format()}" if target is not None else " без целевой суммы"
    return [
        Reply(
            text=(
                f"✅ Цель «{goal_name}»{suffix} создана\n\n"
                "Откладывайте на неё кнопкой «➕ Отложить» — отложенное не будет "
                "считаться доступным для трат."
            ),
            buttons=(
                (
                    Button("➕ Отложить", callback("goal", "allocate", short(goal_id))),
                    Button("🎯 Цели", callback("menu", "goals")),
                ),
            ),
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
        return [Reply(text="⚠️ Цель не найдена.\n\nОткройте список целей заново.")]

    allocated = money(goal.allocated_minor, goal.currency)
    lines = [f"🎯 {goal.name}", "", f"Отложено: {allocated}"]
    if goal.target_minor:
        target = money(goal.target_minor, goal.currency)
        remaining = money(max(0, goal.target_minor - goal.allocated_minor), goal.currency)
        lines.append(f"Цель: {target}")
        lines.append(f"Осталось: {remaining}")
    if goal.due_date:
        lines.append(f"Срок: {format_date(goal.due_date, with_year=True)}")
    if goal.target_minor:
        percent = min(100, goal.allocated_minor * 100 // goal.target_minor)
        lines.append(f"\n{_progress_bar(percent)} {percent}%")
    lines.extend(
        [
            "",
            "➕ Отложить — зарезервировать деньги на цель.",
            "💸 Потрачено — вы купили то, на что копили: резерв уменьшится. Саму "
            "покупку запишите обычной тратой.",
            "↩️ Вернуть — отложенное снова доступно для трат.",
        ]
    )

    code = short(goal.id)
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("➕ Отложить на цель", callback("goal", "allocate", code)),
                    Button("💸 Потрачено", callback("goal", "use", code)),
                ),
                (
                    Button("↩️ Вернуть в бюджет", callback("goal", "release", code)),
                    Button("✏️ Изменить", callback("goal", "edit", code)),
                ),
                (Button("← Цели", callback("menu", "goals")),),
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
    idempotency_key: str | None = None,
) -> list[Reply]:
    """Отложить, использовать или вернуть сумму цели в плане."""
    from fintracker.application.commitments.goals import allocate_to_goal, release_goal, use_goal
    from fintracker.core.errors import DomainError
    from fintracker.core.money import Money
    from fintracker.db.uow import UnitOfWork
    from fintracker.domain.parsing.amounts import parse_amounts

    if text.strip().startswith("-"):
        return [
            Reply(
                text="✍️ Нужна сумма больше нуля\n\nОтправьте положительное число, например 5000.",
                retry_input=True,
            )
        ]
    amounts = parse_amounts(text)
    if not amounts:
        return [
            Reply(
                text="✍️ Не удалось разобрать сумму\n\nОтправьте число, например 5000.",
                retry_input=True,
            )
        ]
    parsed = amounts[0]
    if parsed.value <= 0:
        return [
            Reply(
                text="✍️ Нужна сумма больше нуля\n\nОтправьте положительное число, например 5000.",
                retry_input=True,
            )
        ]
    if parsed.currency is not None and parsed.currency != workspace.currency:
        return [Reply(text=f"ℹ️ Валюта суммы должна быть {workspace.currency}.", retry_input=True)]
    if parsed.is_ambiguous:
        return [
            Reply(
                text=(
                    "✍️ Уточните сумму\n\nНеоднозначная запись числа. Напишите, "
                    "например, 1500 или 1,50."
                ),
                retry_input=True,
            )
        ]
    amount = Money.from_decimal(Decimal(parsed.value), workspace.currency)
    reason = f"telegram:{idempotency_key}" if idempotency_key else "telegram"
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            if operation == "allocate":
                await allocate_to_goal(
                    session, uow, actor=actor, goal_id=goal_id, amount=amount, reason=reason
                )
                result = "На цель отложено"
            elif operation == "use":
                await use_goal(
                    session, uow, actor=actor, goal_id=goal_id, amount=amount, reason=reason
                )
                result = "На цель использовано"
            elif operation == "release":
                await release_goal(
                    session,
                    uow,
                    actor=actor,
                    goal_id=goal_id,
                    amount=amount,
                    reason=reason,
                )
                result = "В бюджет возвращено"
            else:
                return [Reply(text="🔄 Действие недоступно.")]
        except DomainError as exc:
            return [Reply(text=f"⚠️ {exc.message}", retry_input=True)]
    replies = await goal_detail(settings, actor=actor, workspace=workspace, goal_id=goal_id)
    first = replies[0]
    return [Reply(text=f"✅ {result}: {amount.format()}\n\n{first.text}", buttons=first.buttons)]


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
            payload={"step": "name"},
        )
        return [
            Reply(
                text=(
                    "🎯 Новая цель\n\nНа что хотите накопить?\n\n"
                    "Отправьте короткое название, например «Отпуск». Следующим "
                    "сообщением я спрошу сумму."
                ),
                buttons=((Button("✕ Отмена", callback("noop", "nochange")),),),
            )
        ]

    if action == "page" and rest:
        page = int(rest[0]) if rest[0].isdigit() and len(rest[0]) < 8 else 0
        return await goals_view(settings, actor=actor, workspace=workspace, page=page)

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
            return [Reply(text="⚠️ Цель не найдена.\n\nОткройте список целей заново.")]
        return await goal_detail(settings, actor=actor, workspace=workspace, goal_id=goal_id)
    if action in {"allocate", "use", "release"} and rest:
        goal_id = await _resolve_goal(rest[0])
        if goal_id is None:
            return [Reply(text="⚠️ Цель не найдена.\n\nОткройте список целей заново.")]
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind=f"goal_{action}",
            payload={"goal_id": str(goal_id)},
        )
        prompts = {
            "allocate": "➕ Сколько отложить на цель?",
            "use": "💸 Сколько потратили из отложенного?",
            "release": "↩️ Сколько вернуть в бюджет?",
        }
        return [
            Reply(
                text=f"{prompts[action]}\n\nОтправьте число, например 5000.",
                buttons=((Button("✕ Отмена", callback("goal", "open", rest[0])),),),
            )
        ]
    if action == "edit" and rest:
        goal_id = await _resolve_goal(rest[0])
        if goal_id is None:
            return [Reply(text="⚠️ Цель не найдена.\n\nОткройте список целей заново.")]
        return [
            Reply(
                text="✏️ Что изменить в цели?",
                buttons=(
                    (
                        Button("Название", callback("goal", "rename", rest[0])),
                        Button("Сумму цели", callback("goal", "target", rest[0])),
                    ),
                    (Button("🗑 Удалить цель", callback("goal", "close", rest[0])),),
                    (Button("← К цели", callback("goal", "open", rest[0])),),
                ),
            )
        ]
    if action in {"rename", "target"} and rest:
        goal_id = await _resolve_goal(rest[0])
        if goal_id is None:
            return [Reply(text="⚠️ Цель не найдена.\n\nОткройте список целей заново.")]
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind="goal_edit",
            payload={"goal_id": str(goal_id), "field": action},
        )
        prompt = (
            "✏️ Новое название цели\n\nОтправьте его одним сообщением."
            if action == "rename"
            else "🎯 Новая сумма цели\n\nОтправьте число, например 150000."
        )
        return [
            Reply(
                text=prompt,
                buttons=((Button("✕ Отмена", callback("goal", "open", rest[0])),),),
            )
        ]
    if action in {"close", "closeok"} and rest:
        goal_id = await _resolve_goal(rest[0])
        if goal_id is None:
            return [Reply(text="⚠️ Цель не найдена.\n\nОткройте список целей заново.")]
        if action == "close":
            return [
                Reply(
                    text=(
                        "🗑 Удалить цель?\n\nЕсли на ней есть отложенные деньги, сначала "
                        "верните их в бюджет. История взносов сохранится."
                    ),
                    buttons=(
                        (
                            Button("🗑 Удалить", callback("goal", "closeok", rest[0])),
                            Button("Отмена", callback("goal", "open", rest[0])),
                        ),
                    ),
                )
            ]
        from fintracker.application.commitments.goals import close_goal
        from fintracker.core.errors import ConflictError
        from fintracker.db.uow import UnitOfWork

        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
            await uow.lock_workspace(workspace_id, actor=actor)
            try:
                await close_goal(session, uow, actor=actor, goal_id=goal_id)
            except ConflictError as exc:
                return [
                    Reply(
                        text=f"⚠️ {exc.message}",
                        buttons=(
                            (
                                Button("↩️ Вернуть в бюджет", callback("goal", "release", rest[0])),
                                Button("← К цели", callback("goal", "open", rest[0])),
                            ),
                        ),
                    )
                ]
        return [
            Reply(
                text="🗑 Цель удалена.",
                buttons=((Button("🎯 Цели", callback("menu", "goals")),),),
            )
        ]
    return [Reply(text="🔄 Кнопка устарела.\n\nОткройте «Цели» заново.")]


async def apply_goal_edit(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    payload: dict[str, Any],
    text: str,
) -> list[Reply]:
    from fintracker.application.commitments.goals import edit_goal
    from fintracker.application.conversation.guards import single_amount
    from fintracker.core.errors import DomainError
    from fintracker.core.money import Money
    from fintracker.db.uow import UnitOfWork

    goal_id = uuid.UUID(str(payload["goal_id"]))
    field = str(payload.get("field"))
    name: str | None = None
    target: Money | None = None
    if field == "target":
        amount = single_amount(text)
        if amount is None:
            return [Reply(text="✍️ Отправьте сумму числом, например 150000.", retry_input=True)]
        target = Money.from_decimal(amount.value, workspace.currency)
    else:
        name = text.strip()
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        try:
            await edit_goal(session, uow, actor=actor, goal_id=goal_id, name=name, target=target)
        except DomainError as exc:
            return [Reply(text=f"⚠️ {exc.message}", retry_input=True)]
    replies = await goal_detail(settings, actor=actor, workspace=workspace, goal_id=goal_id)
    first = replies[0]
    return [Reply(text=f"✅ Цель изменена\n\n{first.text}", buttons=first.buttons)]


def _progress_bar(percent: int) -> str:
    filled = max(0, min(10, round(percent / 10)))
    return "▓" * filled + "░" * (10 - filled)
