"""Обработка нажатий кнопок (FR-08: устаревшая кнопка не меняет чужую запись)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from fintracker.application.conversation import sections
from fintracker.application.conversation.context import (
    HELP_TEXT,
    active_context,
    load_actor,
    no_budget_reply,
)
from fintracker.application.conversation.keyboards import (
    Button,
    callback,
    main_menu,
    more_menu,
)
from fintracker.application.conversation.types import IncomingMessage, Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import NotFound, PermissionDenied
from fintracker.db.models.access import User, Workspace
from fintracker.db.session import RuntimeRole, session_scope


async def _resolve_uuid(
    settings: Settings, *, workspace_id: uuid.UUID, user_id: uuid.UUID, table: str, prefix: str
) -> uuid.UUID:
    """Восстановить полный ID по короткому префиксу кнопки (LIM-10).

    Поиск ограничен текущим бюджетом: чужой ID через кнопку не проходит.
    """
    from sqlalchemy import text as sql_text

    allowed = {
        "transactions": "transactions",
        "categories": "categories",
        "drafts": "drafts",
        "goals": "goals",
        "recommendations": "recommendations",
        "workspaces": "workspaces",
    }
    if table not in allowed:
        raise NotFound("Объект недоступен")
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        if table == "workspaces":
            statement = sql_text(
                "SELECT w.id FROM workspaces w JOIN memberships m ON m.workspace_id = w.id "
                "WHERE m.user_id = :user_id AND m.status = 'active' "
                "AND replace(w.id::text, '-', '') LIKE :prefix LIMIT 2"
            )
            rows = (
                (await session.execute(statement, {"user_id": user_id, "prefix": f"{prefix}%"}))
                .scalars()
                .all()
            )
        else:
            statement = sql_text(
                f"SELECT id FROM {allowed[table]} "  # noqa: S608 - имя из белого списка
                "WHERE workspace_id = :workspace_id "
                "AND replace(id::text, '-', '') LIKE :prefix LIMIT 2"
            )
            rows = (
                (
                    await session.execute(
                        statement, {"workspace_id": workspace_id, "prefix": f"{prefix}%"}
                    )
                )
                .scalars()
                .all()
            )
    if len(rows) != 1:
        raise NotFound("Кнопка устарела: объект не найден. Откройте раздел заново.")
    resolved = rows[0]
    assert isinstance(resolved, uuid.UUID)
    return resolved


async def dispatch_callback(
    settings: Settings, *, message: IncomingMessage, user_id: uuid.UUID
) -> list[Reply]:
    from fintracker.application.conversation.onboarding_flow import (
        apply_wizard_choice,
        start_join_flow,
    )

    data = message.callback_data or ""
    parts = data.split(":")
    action = parts[0] if parts else ""
    argument = parts[1] if len(parts) > 1 else ""
    rest = parts[2:]

    if action == "noop":
        return [Reply(text="Действие отменено.")]
    if action == "wiz":
        return await apply_wizard_choice(
            settings,
            user_id=user_id,
            action=argument,
            value=rest[0] if rest else "",
            message=message,
        )
    if action == "join":
        return await start_join_flow(settings, user_id=user_id)

    workspace_id = await active_context(settings, user_id=user_id, message=message)

    if action == "ws" and argument == "use" and rest:
        target = await _resolve_uuid(
            settings,
            workspace_id=workspace_id or uuid.UUID(int=0),
            user_id=user_id,
            table="workspaces",
            prefix=rest[0],
        )
        async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
            from fintracker.application.identity.actor import set_active_workspace

            user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
            await set_active_workspace(session, user=user, workspace_id=target)
            workspace = (
                await session.execute(select(Workspace).where(Workspace.id == target))
            ).scalar_one()
            name = workspace.name
        return [
            Reply(
                text=f"Активный бюджет: «{name}».",
                buttons=main_menu(),
            )
        ]

    if workspace_id is None:
        return no_budget_reply()

    actor, workspace = await load_actor(
        settings,
        user_id=user_id,
        workspace_id=workspace_id,
        correlation_id=message.correlation_id,
    )

    match action:
        case "menu":
            return await _menu(settings, actor=actor, workspace=workspace, section=argument)
        case "budget":
            return await _budget_section(
                settings, actor=actor, workspace=workspace, section=argument
            )
        case "cat":
            return await _category_action(
                settings,
                actor=actor,
                workspace=workspace,
                action=argument,
                rest=rest,
                user_id=user_id,
            )
        case "tx":
            return await _transaction_action(
                settings,
                actor=actor,
                workspace=workspace,
                action=argument,
                rest=rest,
                user_id=user_id,
            )
        case "dr":
            return await _draft_action(
                settings,
                actor=actor,
                workspace=workspace,
                action=argument,
                rest=rest,
                user_id=user_id,
            )
        case "fix":
            return await _fix_action(
                settings,
                actor=actor,
                workspace=workspace,
                action=argument,
                rest=rest,
                user_id=user_id,
            )
        case "inv":
            return await _invite_action(settings, actor=actor, workspace=workspace, action=argument)
        case "rec":
            from fintracker.application.conversation.analytics_flow import (
                recommendation_action,
            )

            return await recommendation_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case _:
            return [Reply(text="Кнопка устарела. Откройте нужный раздел заново.")]


async def _menu(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, section: str
) -> list[Reply]:
    from fintracker.application.conversation.analytics_flow import report_view
    from fintracker.application.conversation.goals_flow import goals_view
    from fintracker.application.conversation.io_flow import export_menu
    from fintracker.application.conversation.manual_form import start_manual_form

    match section:
        case "main":
            return [Reply(text="Главное меню", buttons=main_menu())]
        case "more":
            return [Reply(text="Дополнительно", buttons=more_menu())]
        case "budget":
            return await sections.budget_overview(settings, actor=actor, workspace=workspace)
        case "categories":
            return await sections.categories_view(settings, actor=actor, workspace=workspace)
        case "history":
            return await sections.history_view(settings, actor=actor, workspace=workspace)
        case "analytics":
            return await report_view(settings, actor=actor, workspace=workspace)
        case "goals":
            return await goals_view(settings, actor=actor, workspace=workspace)
        case "members":
            return await sections.members_view(settings, actor=actor, workspace=workspace)
        case "settings":
            return await sections.settings_view(settings, actor=actor, workspace=workspace)
        case "io":
            return await export_menu(settings, actor=actor, workspace=workspace)
        case "add":
            return await start_manual_form(settings, actor=actor, workspace=workspace)
        case "budgets":
            return await sections.list_budgets_reply(settings, user_id=actor.user_id)
        case "help":
            return [Reply(text=HELP_TEXT, buttons=main_menu())]
        case _:
            return [Reply(text="Раздел пока недоступен.", buttons=main_menu())]


async def _budget_section(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, section: str
) -> list[Reply]:
    from fintracker.application.planning.periods import list_periods

    workspace_id = actor.require_workspace()
    if section == "explain":
        return [
            Reply(
                text=(
                    "Как посчитано:\n"
                    "• Учтённые расходы — сумма расходных частей текущих ревизий "
                    "минус связанные возвраты.\n"
                    "• Остаток по плану — лимит с учётом принятого переноса минус факт.\n"
                    "• После известных платежей — остаток минус непокрытая часть "
                    "обязательств. Это не баланс счёта.\n"
                    "• Черновики в факт не входят, но показаны отдельно."
                ),
                buttons=((Button("← Бюджет", callback("menu", "budget")),),),
            )
        ]
    if section in {"past", "next"}:
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            periods = await list_periods(session, workspace_id=workspace_id, limit=12)
        lines = ["Периоды бюджета:"]
        for period in periods:
            import datetime as dt

            end = period.end_exclusive - dt.timedelta(days=1)
            marker = " (переходный)" if period.is_transition else ""
            lines.append(
                f"• {period.start_date.isoformat()} — {end.isoformat()} · {period.state}{marker}"
            )
        return [
            Reply(
                text="\n".join(lines),
                buttons=((Button("← Бюджет", callback("menu", "budget")),),),
            )
        ]
    return await sections.budget_overview(settings, actor=actor, workspace=workspace)


async def _category_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
    user_id: uuid.UUID,
) -> list[Reply]:
    from fintracker.application.conversation.category_flow import (
        apply_category_removal,
        category_card,
        manage_categories,
    )

    workspace_id = actor.require_workspace()
    if action == "manage":
        return await manage_categories(settings, actor=actor, workspace=workspace)
    if action == "new":
        return [
            Reply(
                text=(
                    "Напишите «Создай категорию Название». Обязательно только название; "
                    "лимит можно задать позже."
                )
            )
        ]
    if action == "page" and rest:
        return await sections.categories_view(
            settings, actor=actor, workspace=workspace, page=int(rest[0])
        )
    if action in {"open", "arch", "del", "limit", "rename", "move"} and rest:
        category_id = await _resolve_uuid(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            table="categories",
            prefix=rest[0],
        )
        if action == "open":
            return await category_card(
                settings, actor=actor, workspace=workspace, category_id=category_id
            )
        if action == "arch":
            return await apply_category_removal(
                settings,
                actor=actor,
                workspace=workspace,
                category_id=category_id,
                option="archive",
            )
        if action == "del":
            return await apply_category_removal(
                settings,
                actor=actor,
                workspace=workspace,
                category_id=category_id,
                option="delete",
            )
        return [
            Reply(
                text=(
                    "Отправьте новое значение сообщением. Действие применится после подтверждения."
                )
            )
        ]
    return [Reply(text="Действие недоступно.")]


async def _transaction_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
    user_id: uuid.UUID,
) -> list[Reply]:
    from fintracker.application.conversation.corrections import (
        apply_restore,
        apply_void,
    )

    workspace_id = actor.require_workspace()
    if not rest:
        return [Reply(text="Кнопка устарела.")]
    transaction_id = await _resolve_uuid(
        settings,
        workspace_id=workspace_id,
        user_id=user_id,
        table="transactions",
        prefix=rest[0],
    )
    match action:
        case "open":
            return await sections.transaction_card_reply(
                settings, actor=actor, workspace=workspace, transaction_id=transaction_id
            )
        case "void":
            from fintracker.application.conversation.corrections import _propose_void

            return await _propose_void(
                settings, actor=actor, workspace=workspace, target=transaction_id
            )
        case "voidok":
            version = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
            return await apply_void(
                settings,
                actor=actor,
                workspace=workspace,
                transaction_id=transaction_id,
                expected_version=version,
            )
        case "restore":
            return await apply_restore(
                settings, actor=actor, workspace=workspace, transaction_id=transaction_id
            )
        case "note":
            return [
                Reply(
                    text=(
                        "Ответьте на карточку сообщением «Добавь комментарий: текст». "
                        "Комментарий виден всем участникам."
                    )
                )
            ]
        case "edit" | "cat":
            return [
                Reply(
                    text=(
                        "Ответьте на карточку и напишите изменение, например "
                        "«Здесь было 800, а не 1800» или «Перенеси в Продукты»."
                    )
                )
            ]
        case _:
            return [Reply(text="Действие недоступно.")]


async def _draft_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
    user_id: uuid.UUID,
) -> list[Reply]:

    workspace_id = actor.require_workspace()
    if not rest:
        return [Reply(text="Кнопка устарела.")]
    draft_id = await _resolve_uuid(
        settings, workspace_id=workspace_id, user_id=user_id, table="drafts", prefix=rest[0]
    )
    if action == "post":
        return await sections.confirm_draft(
            settings,
            actor=actor,
            workspace=workspace,
            draft_id=draft_id,
            origin="telegram_text",
        )
    if action == "cancel":
        async with session_scope(
            settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
        ) as session:
            from fintracker.application.conversation.entry import load_draft

            draft, candidates = await load_draft(
                session, workspace_id=workspace_id, draft_id=draft_id, owner_id=user_id
            )
            draft.state = "cancelled"
            draft.version += 1
            for candidate in candidates:
                if candidate.state != "posted":
                    candidate.state = "cancelled"
        return [Reply(text="Черновик отменён. Проведённые операции не затронуты.")]
    return [
        Reply(
            text=("Отправьте уточнение сообщением — например, сумму числом или название категории.")
        )
    ]


async def _fix_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
    user_id: uuid.UUID,
) -> list[Reply]:
    from fintracker.application.conversation.corrections import apply_amount_correction

    workspace_id = actor.require_workspace()
    if action != "apply" or len(rest) < 3:
        return [Reply(text="Кнопка устарела.")]
    transaction_id = await _resolve_uuid(
        settings,
        workspace_id=workspace_id,
        user_id=user_id,
        table="transactions",
        prefix=rest[0],
    )
    expected_version = int(rest[1])
    amount = int(rest[2]) if rest[2] != "-" else None
    date_iso = rest[3] if len(rest) > 3 and rest[3] != "-" else None
    return await apply_amount_correction(
        settings,
        actor=actor,
        workspace=workspace,
        transaction_id=transaction_id,
        expected_version=expected_version,
        new_amount_minor=amount,
        new_date_iso=date_iso,
    )


async def _invite_action(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, action: str
) -> list[Reply]:
    from fintracker.application.identity.invites import issue_invite
    from fintracker.db.uow import UnitOfWork

    workspace_id = actor.require_workspace()
    if action != "new":
        return [Reply(text="Действие недоступно.")]
    if not actor.is_admin:
        raise PermissionDenied("Создавать коды приглашений может только администратор")
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        invite = await issue_invite(
            session,
            uow,
            settings=settings,
            workspace_id=workspace_id,
            created_by=actor.user_id,
        )
    return [
        Reply(
            text=(
                f"Код приглашения в бюджет «{workspace.name}»:\n"
                f"{invite.formatted_code}\n\n"
                f"Действует до {invite.expires_at.date().isoformat()}, "
                f"применений: {invite.max_uses}.\n"
                "Передайте код приглашённому сами — бот не пишет первым незнакомым "
                "аккаунтам."
            ),
            buttons=(
                (
                    Button("Участники", callback("menu", "members")),
                    Button("← Меню", callback("menu", "main")),
                ),
            ),
        )
    ]
