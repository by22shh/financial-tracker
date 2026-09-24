"""Обработка нажатий кнопок (FR-08: устаревшая кнопка не меняет чужую запись)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from fintracker.application.conversation import sections, views
from fintracker.application.conversation.context import (
    HELP_TEXT,
    active_context,
    current_status,
    load_actor,
    no_budget_reply,
)
from fintracker.application.conversation.keyboards import (
    KEEP_SOURCE_PREFIX,
    Button,
    budget_selected_menu,
    callback,
    keep,
    main_menu,
    more_menu,
    short,
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
        "budget_periods": "budget_periods",
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

    data = (message.callback_data or "").removeprefix(KEEP_SOURCE_PREFIX)
    parts = data.split(":")
    action = parts[0] if parts else ""
    argument = parts[1] if len(parts) > 1 else ""
    rest = parts[2:]

    if action == "noop":
        return await _noop_reply(settings, user_id=user_id, reason=argument)
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
        already_active = workspace_id == target
        title = "✅ Этот бюджет уже открыт" if already_active else "✅ Бюджет переключён"
        explanation = (
            "Можно продолжать работу."
            if already_active
            else "Новые траты, планы и аналитика теперь относятся к нему."
        )
        return [
            Reply(
                text=f"{title}\n\n📒 {name}\n\n{explanation}",
                buttons=budget_selected_menu(),
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
        case "ws":
            return await _workspace_action(
                settings,
                actor=actor,
                workspace=workspace,
                action=argument,
                rest=rest,
                message=message,
                user_id=user_id,
            )
        case "menu":
            return await _menu(settings, actor=actor, workspace=workspace, section=argument)
        case "nplan":
            from fintracker.application.conversation.analytics_flow import next_plan_view

            page = int(argument) if argument.isdigit() and len(argument) < 8 else 0
            return await next_plan_view(settings, actor=actor, workspace=workspace, page=page)
        case "nlimit":
            return await _next_limit_action(
                settings,
                actor=actor,
                workspace=workspace,
                action=argument,
                rest=rest,
                user_id=user_id,
            )
        case "roll":
            from fintracker.application.conversation.rollover_flow import rollover_action

            return await rollover_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
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
        case "clr":
            from fintracker.application.conversation.clarify import clarify_action

            return await clarify_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case "goal":
            from fintracker.application.conversation.goals_flow import goal_action

            return await goal_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case "pay":
            from fintracker.application.conversation.payments_flow import payment_action

            return await payment_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case "rep":
            from fintracker.application.conversation.analytics_flow import report_slice

            return await report_slice(
                settings, actor=actor, workspace=workspace, slice_name=argument
            )
        case "exp":
            from fintracker.application.conversation.io_flow import export_action

            return await export_action(
                settings,
                actor=actor,
                workspace=workspace,
                fmt=argument,
                chat_id=message.chat_id,
            )
        case "imp":
            from fintracker.application.conversation.io_flow import import_action

            return await import_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case "hist":
            from fintracker.application.conversation.history_flow import history_action

            return await history_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case "set":
            from fintracker.application.conversation.settings_flow import settings_action

            return await settings_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case "mf":
            from fintracker.application.conversation.manual_form import form_action

            return await form_action(
                settings, actor=actor, workspace=workspace, action=argument, rest=rest
            )
        case "cov" if argument in {"ok", "gap"}:
            return await sections.set_completeness(
                settings, actor=actor, workspace=workspace, complete=argument == "ok"
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
            return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]


_NOOP_TEXTS = {
    "keep": "👌 Запись оставлена без изменений.",
    "once": "👌 Готово: изменена только эта запись, правило не сохранено.",
    "keepws": "👌 Бюджет не удалён.",
    "stay": "👌 Вы остаётесь в бюджете.",
    "nopay": "👌 Хорошо, платёж не создан.",
    "nochange": "👌 Хорошо, ничего не изменено.",
}


async def _noop_reply(settings: Settings, *, user_id: uuid.UUID, reason: str) -> list[Reply]:
    """Отказ от действия: понятный итог и дорога дальше, а не тупик."""
    from fintracker.application.conversation.keyboards import back_to_menu
    from fintracker.application.conversation.pending import clear_pending

    # Отказ снимает связанное ожидание ввода, например подтверждение удаления.
    await clear_pending(settings, user_id=user_id)
    text = _NOOP_TEXTS.get(reason, "↩️ Действие отменено.")
    return [Reply(text=text, buttons=back_to_menu())]


async def _menu(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, section: str
) -> list[Reply]:
    from fintracker.application.conversation.analytics_flow import report_view
    from fintracker.application.conversation.goals_flow import goals_view
    from fintracker.application.conversation.io_flow import export_menu
    from fintracker.application.conversation.manual_form import start_manual_form

    match section:
        case "main":
            status = await current_status(settings, actor=actor, workspace=workspace)
            return [
                Reply(
                    text=(
                        f"📒 {workspace.name}\n"
                        f"Период: {views.format_range(status.start_date, status.end_inclusive)}"
                        "\n\nВыберите раздел или просто напишите о трате."
                    ),
                    buttons=main_menu(),
                )
            ]
        case "more":
            return [
                Reply(
                    text=(
                        "🧭 Другие возможности\n\nЗдесь можно управлять бюджетами и "
                        "участниками, планировать платежи и выгружать данные."
                    ),
                    buttons=more_menu(),
                )
            ]
        case "budget":
            return await sections.budget_overview(settings, actor=actor, workspace=workspace)
        case "categories":
            return await sections.categories_view(settings, actor=actor, workspace=workspace)
        case "history":
            return await sections.history_view(settings, actor=actor, workspace=workspace)
        case "analytics":
            return await report_view(settings, actor=actor, workspace=workspace)
        case "review":
            from fintracker.application.conversation.analytics_flow import weekly_review_view

            return await weekly_review_view(settings, actor=actor, workspace=workspace)
        case "summary":
            from fintracker.application.conversation.analytics_flow import period_summary_view

            return await period_summary_view(settings, actor=actor, workspace=workspace)
        case "nextplan":
            from fintracker.application.conversation.analytics_flow import next_plan_view

            return await next_plan_view(settings, actor=actor, workspace=workspace)
        case "goals":
            return await goals_view(settings, actor=actor, workspace=workspace)
        case "members":
            return await sections.members_view(settings, actor=actor, workspace=workspace)
        case "settings":
            return await sections.settings_view(settings, actor=actor, workspace=workspace)
        case "io":
            return await export_menu(settings, actor=actor, workspace=workspace)
        case "payments":
            from fintracker.application.conversation.payments_flow import payments_view

            return await payments_view(settings, actor=actor, workspace=workspace)
        case "add":
            return await start_manual_form(settings, actor=actor, workspace=workspace)
        case "budgets":
            return await sections.list_budgets_reply(settings, user_id=actor.user_id)
        case "help":
            return [Reply(text=HELP_TEXT, buttons=main_menu())]
        case "report":
            # Прежние уведомления ссылались на «report»: ведём в итог периода.
            from fintracker.application.conversation.analytics_flow import period_summary_view

            return await period_summary_view(settings, actor=actor, workspace=workspace)
        case "drafts":
            return await sections.drafts_view(settings, actor=actor, workspace=workspace)
        case "check":
            return await sections.quality_view(settings, actor=actor, workspace=workspace)
        case _:
            return [
                Reply(
                    text="🔄 Кнопка устарела.\n\nВыберите раздел в меню.",
                    buttons=main_menu(),
                )
            ]


async def _budget_section(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, section: str
) -> list[Reply]:
    from fintracker.application.planning.periods import list_periods

    workspace_id = actor.require_workspace()
    if section == "explain":
        return [
            Reply(
                text=(
                    "🧮 Как посчитан бюджет\n\n"
                    "💸 Учтённые расходы\n"
                    "Записанные траты с учётом исправлений, за вычетом связанных возвратов.\n\n"
                    "💰 Остаток по плану\n"
                    "Лимит с учётом принятого переноса минус учтённые расходы.\n\n"
                    "🗓 После известных платежей\n"
                    "Остаток плана за вычетом ещё не оплаченной части плановых платежей.\n\n"
                    "✍️ Записи на уточнение показаны отдельно и в расходы пока не входят.\n\n"
                    "ℹ️ Эти суммы показывают состояние плана. Баланс на счёте может отличаться."
                ),
                buttons=((Button("← Бюджет", callback("menu", "budget")),),),
            )
        ]
    if section == "next":
        from fintracker.application.conversation.analytics_flow import next_plan_view

        return await next_plan_view(settings, actor=actor, workspace=workspace)
    if section == "past":
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            periods = await list_periods(session, workspace_id=workspace_id, limit=12)
        import datetime as dt

        lines = ["📅 Периоды бюджета", ""]
        for period in periods:
            end = period.end_exclusive - dt.timedelta(days=1)
            marker = ", переходный" if period.is_transition else ""
            lines.append(
                f"• {views.format_range(period.start_date, end)} — "
                f"{views.period_state_label(period.state)}{marker}"
            )
        return [
            Reply(
                text="\n".join(lines),
                buttons=((Button("← Бюджет", callback("menu", "budget")),),),
            )
        ]
    return await sections.budget_overview(settings, actor=actor, workspace=workspace)


async def _next_limit_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
    user_id: uuid.UUID,
) -> list[Reply]:
    """Edit limits only in the future period named by the next-plan card."""
    from fintracker.application.conversation.category_flow import (
        begin_future_limit_input,
        future_limits_view,
    )

    if not rest:
        return [Reply(text="🔄 Кнопка устарела. Откройте следующий план заново.")]
    workspace_id = actor.require_workspace()
    period_id = await _resolve_uuid(
        settings,
        workspace_id=workspace_id,
        user_id=user_id,
        table="budget_periods",
        prefix=rest[0],
    )
    if action == "show":
        page = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0
        return await future_limits_view(
            settings,
            actor=actor,
            workspace=workspace,
            period_id=period_id,
            page=page,
        )
    if action == "pick" and len(rest) > 1:
        category_id = await _resolve_uuid(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            table="categories",
            prefix=rest[1],
        )
        return await begin_future_limit_input(
            settings,
            actor=actor,
            workspace=workspace,
            user_id=user_id,
            period_id=period_id,
            category_id=category_id,
        )
    return [Reply(text="🔄 Кнопка устарела. Откройте следующий план заново.")]


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
        choose_reassign_target,
        manage_categories,
    )

    workspace_id = actor.require_workspace()
    if action == "manage":
        return await manage_categories(
            settings,
            actor=actor,
            workspace=workspace,
            page=int(rest[0]) if rest and rest[0].isdigit() else 0,
        )
    if action == "archive":
        from fintracker.application.conversation.category_flow import archived_categories

        return await archived_categories(
            settings,
            actor=actor,
            workspace=workspace,
            page=int(rest[0]) if rest and rest[0].isdigit() else 0,
        )
    if action == "restore" and rest:
        from fintracker.application.conversation.category_flow import apply_category_restore

        category_id = await _resolve_uuid(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            table="categories",
            prefix=rest[0],
        )
        return await apply_category_restore(
            settings, actor=actor, workspace=workspace, category_id=category_id
        )
    if action == "new":
        from fintracker.application.conversation.pending import set_pending

        await set_pending(settings, user_id=user_id, workspace_id=workspace_id, kind="category_new")
        return [
            Reply(
                text=(
                    "🗂 Новая категория\n\nОтправьте её название, например «Путешествия». "
                    "Лимит можно задать следом."
                ),
                buttons=((Button("✕ Отмена", callback("noop", "nochange")),),),
            )
        ]
    if action == "page" and rest:
        return await sections.categories_view(
            settings, actor=actor, workspace=workspace, page=int(rest[0])
        )
    if action == "mv" and len(rest) >= 2:
        source_id = await _resolve_uuid(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            table="categories",
            prefix=rest[0],
        )
        target_id = await _resolve_uuid(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            table="categories",
            prefix=rest[1],
        )
        return await apply_category_removal(
            settings,
            actor=actor,
            workspace=workspace,
            category_id=source_id,
            option="reassign_and_archive",
            reassign_to=target_id,
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
        if action == "move":
            return await choose_reassign_target(
                settings,
                actor=actor,
                workspace=workspace,
                category_id=category_id,
                page=int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0,
            )
        # Обещанное продолжение диалога сохраняется: следующее сообщение
        # применяется к этой категории, а не разбирается как трата (G-13).
        from fintracker.application.conversation.pending import set_pending

        await set_pending(
            settings,
            user_id=user_id,
            workspace_id=workspace_id,
            kind=f"category_{action}",
            payload={"category_id": str(category_id)},
        )
        prompt = (
            "✏️ Новое название категории\n\nОтправьте его одним сообщением."
            if action == "rename"
            else "💰 Лимит на период\n\nОтправьте сумму числом, например 8000. "
            "0 — траты в категории не планируются."
        )
        return [
            Reply(
                text=prompt,
                buttons=((Button("✕ Отмена", callback("cat", "open", rest[0])),),),
            )
        ]
    return [Reply(text="🔄 Действие недоступно.")]


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
        return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]
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
        case "details":
            return await sections.transaction_card_reply(
                settings,
                actor=actor,
                workspace=workspace,
                transaction_id=transaction_id,
                detailed=True,
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
        case "nodel":
            from fintracker.application.conversation.corrections import apply_note

            return await apply_note(
                settings,
                actor=actor,
                workspace=workspace,
                transaction_id=transaction_id,
                note=None,
            )
        case "note" | "edit" | "cat" | "amount" | "date":
            from fintracker.application.conversation.transaction_flow import edit_action

            return await edit_action(
                settings,
                actor=actor,
                workspace=workspace,
                transaction_id=transaction_id,
                action=action,
                rest=rest[1:],
            )
        case _:
            return [Reply(text="🔄 Действие недоступно.")]


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
        return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]
    draft_id = await _resolve_uuid(
        settings, workspace_id=workspace_id, user_id=user_id, table="drafts", prefix=rest[0]
    )
    if action in {"source", "sources", "part", "from", "to", "account", "kind"} or (
        action == "edit" and len(rest) > 1
    ):
        from fintracker.application.conversation.transaction_flow import special_action

        if len(rest) < 3 or not rest[2].isdigit():
            raise NotFound("Кнопка устарела. Откройте черновик заново.")
        if action in {"source", "sources", "part", "from", "to", "kind"} and len(rest) < 4:
            raise NotFound("Кнопка устарела. Откройте черновик заново.")
        return await special_action(
            settings,
            actor=actor,
            workspace=workspace,
            draft_id=draft_id,
            action=action,
            candidate_prefix=rest[1],
            expected_version=int(rest[2]),
            rest=rest[3:],
        ) or await sections.draft_reply(
            settings, actor=actor, workspace=workspace, draft_id=draft_id
        )
    if action == "cats":
        page = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0
        return await sections.draft_categories(
            settings, actor=actor, workspace=workspace, draft_id=draft_id, page=page
        )
    if action == "setcat" and len(rest) > 1:
        category_id = (
            None
            if rest[1] == "-"
            else await _resolve_uuid(
                settings,
                workspace_id=workspace_id,
                user_id=user_id,
                table="categories",
                prefix=rest[1],
            )
        )
        return await sections.set_draft_category(
            settings, actor=actor, workspace=workspace, draft_id=draft_id, category_id=category_id
        )
    if action == "open":
        return await sections.draft_reply(
            settings, actor=actor, workspace=workspace, draft_id=draft_id
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
        from sqlalchemy import delete

        from fintracker.db.models.platform import PendingAction

        already_posted = False
        async with session_scope(
            settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
        ) as session:
            from fintracker.application.conversation.entry import load_draft
            from fintracker.db.uow import UnitOfWork

            uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
            await uow.lock_workspace(workspace_id, actor=actor)
            draft, candidates = await load_draft(
                session, workspace_id=workspace_id, draft_id=draft_id, owner_id=user_id
            )
            already_posted = draft.state == "posted"
            await session.execute(
                delete(PendingAction).where(
                    PendingAction.user_id == user_id,
                    PendingAction.workspace_id == workspace_id,
                    PendingAction.payload["draft_id"].astext == str(draft_id),
                )
            )
            if not already_posted:
                draft.state = "cancelled"
                draft.version += 1
                for candidate in candidates:
                    if candidate.state != "posted":
                        candidate.state = "cancelled"
        if already_posted:
            return await sections.confirm_draft(
                settings,
                actor=actor,
                workspace=workspace,
                draft_id=draft_id,
                origin="telegram_text",
            )
        return [
            Reply(
                text="↩️ Запись отменена\n\nНичего не сохранено.",
                buttons=((Button("🏠 Меню", callback("menu", "main")),),),
            )
        ]
    if action == "retry":
        from fintracker.application.intelligence.media_pipeline import retry_media_draft

        return await retry_media_draft(
            settings, actor=actor, workspace=workspace, draft_id=draft_id
        )
    if action == "paid":
        from fintracker.application.intelligence.media_pipeline import confirm_invoice_paid

        return await confirm_invoice_paid(
            settings, actor=actor, workspace=workspace, draft_id=draft_id
        )
    if action == "edit":
        # Правка идёт в тот же черновик: следующее сообщение меняет его, а не
        # создаёт вторую запись (FR-20, G-16).
        from fintracker.application.conversation.pending import set_pending

        await set_pending(
            settings,
            user_id=user_id,
            workspace_id=workspace_id,
            kind="draft_edit",
            payload={"draft_id": str(draft_id)},
        )
        return [
            Reply(
                text=(
                    "✏️ Что изменить?\n\nОтправьте одним сообщением новую сумму («600»), "
                    "дату («вчера», «20.09») или название категории.\n\n"
                    "Изменится эта же запись, новая не появится."
                ),
                buttons=(
                    (
                        Button("🗂 Выбрать категорию", callback("dr", "cats", short(draft_id), "0")),
                        Button("← К записи", callback("dr", "open", short(draft_id))),
                    ),
                ),
            )
        ]
    return [
        Reply(
            text=(
                "✍️ Отправьте уточнение сообщением — например, сумму числом или название категории."
            )
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
    from fintracker.application.conversation.corrections import (
        apply_amount_correction,
        apply_category_correction,
        create_category_and_move,
        remember_category_rule,
    )

    workspace_id = actor.require_workspace()
    if action not in {"apply", "cat", "rule", "newcat"} or len(rest) < 2:
        return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]
    transaction_id = await _resolve_uuid(
        settings,
        workspace_id=workspace_id,
        user_id=user_id,
        table="transactions",
        prefix=rest[0],
    )
    if action == "newcat":
        if len(rest) < 3:
            return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]
        return await create_category_and_move(
            settings,
            actor=actor,
            workspace=workspace,
            transaction_id=transaction_id,
            expected_version=int(rest[1]),
            name=":".join(rest[2:]),
        )
    if action == "rule":
        category_id = await _resolve_uuid(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            table="categories",
            prefix=rest[1],
        )
        return await remember_category_rule(
            settings,
            actor=actor,
            workspace=workspace,
            transaction_id=transaction_id,
            category_id=category_id,
        )
    if action == "cat":
        if len(rest) < 3:
            return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]
        category_id = await _resolve_uuid(
            settings,
            workspace_id=workspace_id,
            user_id=user_id,
            table="categories",
            prefix=rest[2],
        )
        return await apply_category_correction(
            settings,
            actor=actor,
            workspace=workspace,
            transaction_id=transaction_id,
            expected_version=int(rest[1]),
            category_id=category_id,
        )
    if len(rest) < 3:
        return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]
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
    from zoneinfo import ZoneInfo

    from fintracker.application.identity.invites import issue_invite
    from fintracker.db.uow import UnitOfWork

    workspace_id = actor.require_workspace()
    if action != "new":
        return [Reply(text="🔄 Кнопка устарела.\n\nОткройте раздел «Участники» заново.")]
    if not actor.is_admin:
        raise PermissionDenied("Приглашать в бюджет может только администратор")
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        invite = await issue_invite(
            session,
            uow,
            settings=settings,
            workspace_id=workspace_id,
            created_by=actor.user_id,
        )
    expires = invite.expires_at.astimezone(ZoneInfo(workspace.timezone)).date()
    lines = [
        f"🔑 Приглашение в бюджет «{workspace.name}»",
        "",
        "Код:",
        invite.formatted_code,
    ]
    username = settings.telegram.bot_username.strip().lstrip("@")
    if username:
        token = invite.formatted_code.replace("-", "")
        lines.extend(["", "Или ссылка — откроет бот и сразу добавит в бюджет:"])
        lines.append(f"https://t.me/{username}?start=join_{token}")
    lines.extend(
        [
            "",
            f"Действует до {views.format_date(expires, with_year=True)}, "
            f"входов по коду: {invite.max_uses}.",
            "",
            "Перешлите это сообщение человеку, которого хотите пригласить. В боте "
            "нужно нажать «🔑 Войти по коду» и отправить код.",
        ]
    )
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("👥 Участники", keep(callback("menu", "members"))),
                    Button("🏠 Меню", keep(callback("menu", "main"))),
                ),
            ),
        )
    ]


async def _workspace_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
    message: IncomingMessage,
    user_id: uuid.UUID,
) -> list[Reply]:
    """Выход, передача роли, исключение и удаление бюджета (FR-80–FR-83)."""
    from fintracker.application.conversation.pending import set_pending
    from fintracker.application.identity.membership import (
        accept_admin_transfer,
        deletion_preview,
        leave_workspace,
        list_members,
        propose_admin_transfer,
        remove_member,
    )
    from fintracker.core.errors import ConflictError
    from fintracker.db.uow import UnitOfWork

    workspace_id = actor.require_workspace()
    correlation = message.correlation_id or uuid.uuid4().hex
    members_button = (Button("👥 Участники", callback("menu", "members")),)

    match action:
        case "leave":
            if actor.is_admin:
                # Действующий бюджет не остаётся без администратора (A172).
                return [
                    Reply(
                        text=(
                            "ℹ️ Вы администратор этого бюджета\n\nЧтобы выйти, сначала "
                            "передайте управление другому участнику. Если бюджет больше не "
                            "нужен, его можно удалить."
                        ),
                        buttons=(
                            (
                                Button("👑 Передать роль", callback("ws", "transfer")),
                                Button("🗑 Удалить бюджет", callback("ws", "delete")),
                            ),
                            members_button,
                        ),
                    )
                ]
            return [
                Reply(
                    text=(
                        f"🚪 Выйти из бюджета «{workspace.name}»?\n\nВаши записи останутся в "
                        "общей истории, но доступ к бюджету пропадёт. Вернуться можно по "
                        "новому приглашению."
                    ),
                    buttons=(
                        (
                            Button("🚪 Выйти", callback("ws", "leaveok")),
                            Button("Остаться", callback("noop", "stay")),
                        ),
                    ),
                )
            ]
        case "leaveok":
            try:
                await leave_workspace(
                    settings,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    correlation_id=correlation,
                )
            except ConflictError as exc:
                return [Reply(text=f"⚠️ {exc.message}", buttons=(members_button,))]
            return [
                Reply(
                    text=(
                        f"🚪 Вы вышли из бюджета «{workspace.name}»\n\n"
                        "Созданные вами записи остались в общей истории."
                    ),
                    buttons=((Button("📒 Мои бюджеты", callback("menu", "budgets")),),),
                )
            ]
        case "transfer" | "remove":
            if not actor.is_admin:
                raise PermissionDenied("Это действие доступно только администратору бюджета")
            async with session_scope(
                settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
            ) as session:
                members = await list_members(session, workspace_id=workspace_id)
            candidates = [
                item
                for item in members
                if item.user_id != actor.user_id and item.status.value == "active"
            ]
            if not candidates:
                return [
                    Reply(
                        text=(
                            "ℹ️ В бюджете пока нет других участников\n\n"
                            "Пригласите человека кодом из раздела «Участники»."
                        ),
                        buttons=(members_button,),
                    )
                ]
            target_action = "transferto" if action == "transfer" else "removeask"
            rows = [
                (
                    Button(
                        item.display_name[:40], callback("ws", target_action, item.user_id.hex[:16])
                    ),
                )
                for item in candidates[:12]
            ]
            rows.append(members_button)
            title = (
                "👑 Кому передать управление?\n\nРоль перейдёт после того, как участник её "
                "примет. До этого администратор — вы."
                if action == "transfer"
                else "➖ Кого исключить из бюджета?\n\nЕго записи останутся в истории."
            )
            return [Reply(text=title, buttons=tuple(rows))]
        case "removeask" | "removeok":
            if not actor.is_admin:
                raise PermissionDenied("Исключать участников может только администратор")
            if not rest:
                return [Reply(text="🔄 Кнопка устарела.\n\nОткройте «Участники» заново.")]
            target = await _resolve_user(
                settings, workspace_id=workspace_id, user_id=user_id, prefix=rest[0]
            )
            name = await _member_name(settings, actor=actor, target_user_id=target)
            if action == "removeask":
                return [
                    Reply(
                        text=(
                            f"➖ Исключить {name} из бюджета?\n\nУчастник потеряет доступ и "
                            "не сможет войти по старому коду. Его записи останутся в истории."
                        ),
                        buttons=(
                            (
                                Button("➖ Исключить", callback("ws", "removeok", rest[0])),
                                Button("Отмена", callback("menu", "members")),
                            ),
                        ),
                    )
                ]
            await remove_member(
                settings,
                workspace_id=workspace_id,
                admin_user_id=actor.user_id,
                target_user_id=target,
                correlation_id=correlation,
            )
            return [
                Reply(
                    text=f"✅ {name} больше не участвует в бюджете «{workspace.name}».",
                    buttons=(members_button,),
                )
            ]
        case "transferto":
            if not rest:
                return [Reply(text="🔄 Кнопка устарела.\n\nОткройте «Участники» заново.")]
            target = await _resolve_user(
                settings, workspace_id=workspace_id, user_id=user_id, prefix=rest[0]
            )
            async with session_scope(
                settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
            ) as session:
                uow = UnitOfWork(session=session, correlation_id=correlation)
                await propose_admin_transfer(
                    session,
                    uow,
                    workspace_id=workspace_id,
                    from_user_id=actor.user_id,
                    to_user_id=target,
                )
            name = await _member_name(settings, actor=actor, target_user_id=target)
            return [
                Reply(
                    text=(
                        f"✅ Предложение отправлено: {name}\n\nУчастник получит сообщение с "
                        "кнопками «Принять роль» и «Отказаться». До ответа администратор — вы."
                    ),
                    buttons=(members_button,),
                )
            ]
        case "acceptadmin":
            async with session_scope(
                settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
            ) as session:
                from fintracker.db.models.access import AdminTransferProposal

                pending_id = (
                    await session.execute(
                        select(AdminTransferProposal.id).where(
                            AdminTransferProposal.workspace_id == workspace_id,
                            AdminTransferProposal.to_user_id == user_id,
                            AdminTransferProposal.state == "pending",
                        )
                    )
                ).scalar_one_or_none()
            if pending_id is None:
                return [
                    Reply(
                        text="ℹ️ Предложение уже неактуально: его приняли, отклонили или отозвали.",
                        buttons=(members_button,),
                    )
                ]
            try:
                await accept_admin_transfer(
                    settings,
                    proposal_id=pending_id,
                    acting_user_id=user_id,
                    correlation_id=correlation,
                    workspace_id=workspace_id,
                )
            except ConflictError as exc:
                return [Reply(text=f"⚠️ {exc.message}", buttons=(members_button,))]
            return [
                Reply(
                    text=(
                        f"👑 Вы стали администратором бюджета «{workspace.name}»\n\n"
                        "Вы можете приглашать и исключать участников, менять настройки "
                        "бюджета и удалять его."
                    ),
                    buttons=(members_button,),
                )
            ]
        case "declineadmin":
            # Отказ закрывает предложение: администратор остаётся прежним.
            async with session_scope(
                settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
            ) as session:
                from fintracker.db.models.access import AdminTransferProposal

                declined = (
                    await session.execute(
                        select(AdminTransferProposal)
                        .where(
                            AdminTransferProposal.workspace_id == workspace_id,
                            AdminTransferProposal.to_user_id == user_id,
                            AdminTransferProposal.state == "pending",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if declined is None:
                    return [
                        Reply(
                            text="ℹ️ Предложение уже неактуально.",
                            buttons=(members_button,),
                        )
                    ]
                from datetime import UTC, datetime

                declined.state = "declined"
                declined.resolved_at = datetime.now(UTC)
                declined.version += 1
            return [
                Reply(
                    text="👌 Вы отказались от роли. Администратор бюджета не изменился.",
                    buttons=(members_button,),
                )
            ]
        case "delete":
            if not actor.is_admin:
                raise PermissionDenied("Удалить бюджет может только администратор")
            async with session_scope(
                settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
            ) as session:
                preview = await deletion_preview(
                    session, workspace_id=workspace_id, admin_user_id=actor.user_id
                )
            # Следующее сообщение сверяется с названием только после этой кнопки.
            await set_pending(
                settings,
                user_id=user_id,
                workspace_id=workspace_id,
                kind="workspace_delete",
                payload={},
            )
            others = max(0, preview.member_count - 1)
            members_line = (
                f"Доступ потеряют ещё {others} "
                f"{views.plural(others, 'участник', 'участника', 'участников')}."
                if others
                else "Других участников в бюджете нет."
            )
            return [
                Reply(
                    text=(
                        f"🗑 Удалить бюджет «{preview.name}»?\n\n"
                        "Будут удалены без возможности восстановления:\n"
                        f"• операции: {preview.transaction_count}\n"
                        f"• категории: {preview.category_count}\n"
                        f"• цели: {preview.goal_count}\n"
                        f"• вложения: {preview.attachment_count}\n\n"
                        f"{members_line}\n\n"
                        "Чтобы подтвердить, отправьте название бюджета:\n"
                        f"{preview.name}"
                    ),
                    buttons=((Button("✕ Отмена", callback("noop", "keepws")),),),
                )
            ]
        case _:
            return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]


async def _member_name(
    settings: Settings, *, actor: ActorContext, target_user_id: uuid.UUID
) -> str:
    from fintracker.application.identity.membership import list_members

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        members = await list_members(session, workspace_id=workspace_id)
    return next(
        (item.display_name for item in members if item.user_id == target_user_id),
        "Участник",
    )


async def _resolve_user(
    settings: Settings, *, workspace_id: uuid.UUID, user_id: uuid.UUID, prefix: str
) -> uuid.UUID:
    """Найти участника этого бюджета по короткому префиксу (LIM-10)."""
    from sqlalchemy import text as sql_text

    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        rows = list(
            (
                await session.execute(
                    sql_text(
                        "SELECT user_id FROM memberships WHERE workspace_id = :workspace_id "
                        "AND status = 'active' "
                        "AND replace(user_id::text, '-', '') LIKE :prefix LIMIT 2"
                    ),
                    {"workspace_id": workspace_id, "prefix": f"{prefix}%"},
                )
            )
            .scalars()
            .all()
        )
    if len(rows) != 1:
        raise NotFound("Участник не найден в этом бюджете")
    resolved = rows[0]
    assert isinstance(resolved, uuid.UUID)
    return resolved
