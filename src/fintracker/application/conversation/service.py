"""Маршрутизатор пользовательских путей (раздел 6 ТЗ, NFR-02, NFR-03).

Один сервис обслуживает Telegram, ручной ввод и будущий Mini App: бизнес-правила
не дублируются в интерфейсе (ADR-10).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import replace as dataclass_replace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from fintracker.application.conversation import sections, views
from fintracker.application.conversation.context import (
    HELP_TEXT,
    active_context,
    current_status,
    load_actor,
    no_budget_reply,
)
from fintracker.application.conversation.entry import (
    CandidateFields,
    DraftAlreadyExists,
    ExtractionResult,
    create_draft_with_candidates,
    extract_from_text,
    find_message_draft,
)
from fintracker.application.conversation.keyboards import (
    Button,
    callback,
    main_menu,
    start_menu,
)
from fintracker.application.conversation.types import IncomingMessage, MessageKind, Reply
from fintracker.application.identity.actor import ensure_user, list_budgets
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import (
    DomainError,
    ProviderUnavailable,
    QuotaExceeded,
    TemporarilyUnavailable,
    ValidationFailed,
)
from fintracker.core.logging import get_logger
from fintracker.core.money import Money
from fintracker.db.models.access import Membership, Workspace
from fintracker.db.models.platform import Draft
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.parsing.intent import Intent

logger = get_logger("conversation")


# Команда пользователя не отказывает из-за идущего рядом изменения доступа:
# короткое ожидание выполняется за него (ADR-14, G-30).
ACCESS_RETRIES = 5
ACCESS_RETRY_DELAY = 0.2


async def handle(settings: Settings, message: IncomingMessage) -> list[Reply]:
    """Обработать входящее сообщение и вернуть ответы пользователю."""
    from fintracker.db.uow import ACCESS_CHANGE

    for attempt in range(ACCESS_RETRIES):
        try:
            return await _route(settings, message)
        except TemporarilyUnavailable as exc:
            blocked = (exc.details or {}).get("reason") == ACCESS_CHANGE
            if not blocked or attempt == ACCESS_RETRIES - 1:
                logger.info(
                    "conversation_domain_error",
                    code=exc.code.value,
                    correlation_id=message.correlation_id,
                )
                return [error_reply(exc.message)]
            await asyncio.sleep(ACCESS_RETRY_DELAY * (attempt + 1))
        except DomainError as exc:
            logger.info(
                "conversation_domain_error",
                code=exc.code.value,
                correlation_id=message.correlation_id,
            )
            return [error_reply(exc.message)]
    return [
        Reply(
            text=(
                "⏳ Бюджет сейчас обновляет доступ участников\n\n"
                "Повторите действие через несколько секунд."
            )
        )
    ]


def error_reply(message: str) -> Reply:
    """Понятная ошибка с дорогой назад: действие не заканчивается тупиком."""
    body = message.strip()
    if body and body[-1] not in ".!?…»)":
        body += "."
    return Reply(
        text=f"⚠️ {body}",
        buttons=((Button("🏠 Меню", callback("menu", "main")),),),
    )


async def _route(settings: Settings, message: IncomingMessage) -> list[Reply]:
    async with session_scope(settings, RuntimeRole.API) as session:
        user = await ensure_user(session, telegram_user_id=message.telegram_user_id)
        user_id = user.id

    if message.display_name:
        await _link_profile(settings, user_id=user_id, message=message)

    if message.kind is MessageKind.CALLBACK and message.callback_data:
        from fintracker.application.conversation.callbacks import dispatch_callback

        return await dispatch_callback(settings, message=message, user_id=user_id)

    command = message.command
    if command:
        return await _handle_command(settings, message, user_id, command)

    if message.kind in {MessageKind.VOICE, MessageKind.PHOTO, MessageKind.DOCUMENT}:
        from fintracker.application.conversation.media import handle_media

        return await handle_media(settings, message, user_id=user_id)

    if message.kind is MessageKind.TEXT and message.text:
        return await _handle_free_text(settings, message, user_id)

    return not_understood_reply(None)


def not_understood_reply(text: str | None) -> list[Reply]:
    """Непонятое сообщение: пример с тем же словом и дорога в меню."""
    words = (text or "").strip()
    example = f"«{words[:30]} 250»" if words and len(words.split()) <= 3 else "«кофе 250»"
    return [
        Reply(
            text=(
                "🤔 Не понял, что сделать\n\n"
                f"Чтобы записать трату, добавьте сумму: {example}.\n"
                "Остальное — в меню или в /help."
            ),
            buttons=(
                (
                    Button("🏠 Меню", callback("menu", "main")),
                    Button("❔ Помощь", callback("menu", "help")),
                ),
            ),
        )
    ]


async def _link_profile(
    settings: Settings, *, user_id: uuid.UUID, message: IncomingMessage
) -> None:
    """Подписать участника его именем из Telegram в текущем бюджете (FR-04)."""
    from fintracker.application.identity.profile import ensure_member_profile

    try:
        workspace_id = await active_context(settings, user_id=user_id, message=message)
        if workspace_id is not None:
            await ensure_member_profile(
                settings,
                user_id=user_id,
                workspace_id=workspace_id,
                name=message.display_name,
            )
    except DomainError:
        # Профиль — удобство подписи: его сбой не мешает самой команде.
        logger.info("profile_link_skipped", correlation_id=message.correlation_id)


async def _handle_command(
    settings: Settings, message: IncomingMessage, user_id: uuid.UUID, command: str
) -> list[Reply]:
    from fintracker.application.conversation.onboarding_flow import (
        start_join_flow,
        submit_join_code,
    )

    argument = message.command_argument

    if command == "/start":
        if argument.startswith("join_"):
            return await submit_join_code(
                settings, user_id=user_id, raw_code=argument[5:], message=message
            )
        if message.invite_digest:
            return await submit_join_code(
                settings, user_id=user_id, code_digest=message.invite_digest, message=message
            )
        from fintracker.application.conversation.onboarding_flow import has_active_wizard

        async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
            budgets = await list_budgets(session, user_id)
        # Повторный /start не создаёт второй бюджет и не сбрасывает планы (FR-05).
        unfinished = await has_active_wizard(settings, user_id=user_id)
        if budgets:
            active_budget = next(
                (item for item in budgets if item.is_active_context and item.state == "active"),
                None,
            )
            if active_budget is not None:
                actor, workspace = await load_actor(
                    settings,
                    user_id=user_id,
                    workspace_id=active_budget.workspace_id,
                    correlation_id=message.correlation_id,
                )
                status = await current_status(settings, actor=actor, workspace=workspace)
                text = (
                    "👋 С возвращением!\n\n"
                    f"📒 Бюджет: {workspace.name}\n"
                    f"Период: {views.format_range(status.start_date, status.end_inclusive)}\n\n"
                    "Чтобы записать трату, просто напишите её: «кофе 250»."
                )
            else:
                text = "👋 С возвращением!\n\nВыберите бюджет, с которым хотите продолжить работу."
            if unfinished:
                text += "\n\nЕсть незавершённая настройка нового бюджета — её можно продолжить."
            return [
                Reply(
                    text=text,
                    buttons=start_menu(
                        returning=True,
                        unfinished=unfinished,
                        active=active_budget is not None,
                    ),
                )
            ]
        if unfinished:
            return [
                Reply(
                    text=(
                        "👋 С возвращением!\n\nНастройка бюджета ещё не завершена. Всё, "
                        "что вы уже указали, сохранено — продолжим с того же шага?"
                    ),
                    buttons=start_menu(returning=False, unfinished=True),
                )
            ]
        return [
            Reply(
                text=(
                    "👋 Добро пожаловать в «Бюджет»!\n\nЗдесь удобно записывать траты,"
                    " следить за лимитами и планировать накопления — самостоятельно"
                    " или вместе.\n\n📒 Создайте свой бюджет\nНастройте категории, "
                    "доход и период учёта.\n\n🔑 Или присоединитесь к общему\nПопросите"
                    " у администратора код приглашения и введите его здесь."
                ),
                buttons=start_menu(returning=False),
            )
        ]

    if command == "/help":
        from fintracker.application.conversation.onboarding_flow import wizard_help

        current_help = await wizard_help(settings, user_id=user_id)
        if current_help is not None:
            return current_help
        return [Reply(text=HELP_TEXT, buttons=main_menu())]

    if command == "/join":
        if argument:
            return await submit_join_code(
                settings, user_id=user_id, raw_code=argument, message=message
            )
        if message.invite_digest:
            return await submit_join_code(
                settings, user_id=user_id, code_digest=message.invite_digest, message=message
            )
        return await start_join_flow(settings, user_id=user_id)

    if command == "/budgets":
        return await sections.list_budgets_reply(settings, user_id=user_id)

    workspace_id = await active_context(settings, user_id=user_id, message=message)
    if command == "/cancel":
        return await _cancel_active(settings, user_id=user_id, workspace_id=workspace_id)
    if workspace_id is None:
        return no_budget_reply()

    actor, workspace = await load_actor(
        settings,
        user_id=user_id,
        workspace_id=workspace_id,
        correlation_id=message.correlation_id,
    )

    match command:
        case "/budget":
            return await sections.budget_overview(settings, actor=actor, workspace=workspace)
        case "/categories":
            return await sections.categories_view(settings, actor=actor, workspace=workspace)
        case "/history":
            return await sections.history_view(
                settings,
                actor=actor,
                workspace=workspace,
                note_query=message.command_argument or None,
            )
        case "/report":
            from fintracker.application.conversation.analytics_flow import report_view

            return await report_view(settings, actor=actor, workspace=workspace)
        case "/review":
            from fintracker.application.conversation.analytics_flow import weekly_review_view

            return await weekly_review_view(settings, actor=actor, workspace=workspace)
        case "/summary":
            from fintracker.application.conversation.analytics_flow import period_summary_view

            return await period_summary_view(settings, actor=actor, workspace=workspace)
        case "/plan":
            from fintracker.application.conversation.analytics_flow import next_plan_view

            return await next_plan_view(settings, actor=actor, workspace=workspace)
        case "/members":
            return await sections.members_view(settings, actor=actor, workspace=workspace)
        case "/goals":
            from fintracker.application.conversation.goals_flow import goals_view

            return await goals_view(settings, actor=actor, workspace=workspace)
        case "/settings":
            return await sections.settings_view(settings, actor=actor, workspace=workspace)
        case "/export":
            from fintracker.application.conversation.io_flow import export_menu

            return await export_menu(settings, actor=actor, workspace=workspace)
        case "/payments":
            from fintracker.application.conversation.payments_flow import payments_view

            return await payments_view(settings, actor=actor, workspace=workspace)
        case "/add":
            from fintracker.application.conversation.manual_form import start_manual_form

            return await start_manual_form(settings, actor=actor, workspace=workspace)
        case _:
            return [
                Reply(
                    text="🤔 Такой команды нет\n\nСписок команд — в /help, разделы — в меню.",
                    buttons=main_menu(),
                )
            ]


async def _cancel_active(
    settings: Settings, *, user_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> list[Reply]:
    """`/cancel` отменяет активный диалог или черновик, но не проведённую запись."""
    from fintracker.application.conversation.onboarding_flow import cancel_wizard
    from fintracker.application.conversation.pending import clear_pending

    if await cancel_wizard(settings, user_id=user_id):
        return [
            Reply(
                text=(
                    "↩️ Настройка бюджета отменена\n\nУже созданные бюджеты и записи не изменились."
                ),
                buttons=start_menu(returning=workspace_id is not None),
            )
        ]
    from fintracker.application.conversation.keyboards import back_to_menu
    from fintracker.application.conversation.pending import peek_pending

    pending = await peek_pending(settings, user_id=user_id, workspace_id=workspace_id)
    await clear_pending(settings, user_id=user_id, workspace_id=workspace_id)
    if pending is not None:
        return [
            Reply(
                text=(
                    "↩️ Ввод отменён\n\nНичего не изменено. Можно выбрать раздел или записать трату."
                ),
                buttons=back_to_menu(),
            )
        ]
    if workspace_id is None:
        return [Reply(text="✅ Отменять нечего.", buttons=start_menu(returning=False))]
    # Несохранённые записи не отменяются скопом: каждая видна в своём
    # списке и отменяется отдельно, чтобы не потерять нужную.
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        from sqlalchemy import func

        waiting = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Draft)
                    .where(
                        Draft.workspace_id == workspace_id,
                        Draft.owner_user_id == user_id,
                        Draft.state.in_(("needs_clarification", "ready")),
                    )
                )
            ).scalar_one()
        )
    if waiting:
        return [
            Reply(
                text=(
                    "✅ Отменять нечего\n\n"
                    f"Есть несохранённые записи: {waiting}. Их можно открыть и сохранить "
                    "или отменить по одной."
                ),
                buttons=(
                    (Button("✍️ Открыть несохранённые", callback("menu", "drafts")),),
                    *back_to_menu(),
                ),
            )
        ]
    return [
        Reply(
            text="✅ Отменять нечего. Можно записать новую трату или выбрать раздел.",
            buttons=back_to_menu(),
        )
    ]


async def _handle_free_text(
    settings: Settings, message: IncomingMessage, user_id: uuid.UUID
) -> list[Reply]:
    from fintracker.application.conversation.guards import (
        invite_code_in_text,
        is_greeting,
        looks_like_new_entry,
    )
    from fintracker.application.conversation.onboarding_flow import (
        continue_wizard_input,
        has_active_wizard,
        submit_join_code,
    )

    assert message.text is not None
    text = message.text.strip()

    if text.casefold().strip(" .!") in {"отмена", "отменить", "стоп", "cancel"}:
        cancel_workspace = await active_context(settings, user_id=user_id, message=message)
        return await _cancel_active(settings, user_id=user_id, workspace_id=cancel_workspace)

    # Код приглашения после кнопки «Войти по коду» — обычный текст (FR-78).
    code = invite_code_in_text(text)
    if code is not None:
        return await submit_join_code(settings, user_id=user_id, raw_code=code, message=message)

    workspace_id = await active_context(settings, user_id=user_id, message=message)
    wizard_notice: str | None = None
    if await has_active_wizard(settings, user_id=user_id):
        handled = await continue_wizard_input(
            settings, user_id=user_id, message=message, has_budget=workspace_id is not None
        )
        if handled is not None:
            return handled
        if workspace_id is not None and looks_like_new_entry(text):
            wizard_notice = (
                "ℹ️ Настройка нового бюджета не потеряна: продолжить её можно через "
                "/start → «Продолжить настройку»."
            )

    if workspace_id is None:
        if is_greeting(text):
            return await _handle_command(settings, message, user_id, "/start")
        return no_budget_reply()

    actor, workspace = await load_actor(
        settings,
        user_id=user_id,
        workspace_id=workspace_id,
        correlation_id=message.correlation_id,
    )

    # Кнопка, обещавшая продолжение, получает следующее сообщение (G-13…G-16).
    # Самостоятельная трата не должна тихо попасть в чужое поле ввода.
    abandoned = await _abandon_pending_for_new_entry(settings, actor=actor, text=text)
    if not abandoned:
        continued = await _continue_pending(
            settings, actor=actor, workspace=workspace, message=message
        )
        if continued is not None:
            return continued

    replies = await _free_text_in_budget(
        settings, actor=actor, workspace=workspace, message=message
    )
    notice = (
        "ℹ️ Прошлое действие отменено: сообщение записано как новая трата."
        if abandoned
        else wizard_notice
    )
    if notice and replies:
        first = replies[0]
        replies[0] = dataclass_replace(first, text=f"{notice}\n\n{first.text}")
    return replies


async def _free_text_in_budget(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply]:
    """Свободный текст в выбранном бюджете после всех ожидаемых ответов."""
    from fintracker.application.conversation.guards import bare_date, is_greeting

    assert message.text is not None
    text = message.text.strip()
    if is_greeting(text):
        return await _main_menu_reply(settings, actor=actor, workspace=workspace, greeting=True)

    from fintracker.application.conversation.clarify import try_answer_open_question

    answered = await try_answer_open_question(
        settings, actor=actor, workspace=workspace, message=message
    )
    if answered is not None:
        return answered

    date_only = bare_date(text)
    if date_only is not None:
        # «25.09» без контекста — дата, а не трата на 25,09 (FR-10).
        return [
            Reply(
                text=(
                    f"📅 Похоже, это дата: {views.format_date(date_only)}\n\n"
                    "Чтобы записать трату, отправьте описание и сумму, например "
                    "«такси 450». Дату можно добавить в начало: «вчера такси 450»."
                ),
                buttons=((Button("🏠 Меню", callback("menu", "main")),),),
            )
        ]

    from fintracker.application.conversation.corrections import try_handle_correction

    corrected = await try_handle_correction(
        settings, actor=actor, workspace=workspace, message=message
    )
    if corrected is not None:
        return corrected

    if "|" in text:
        from fintracker.application.conversation.manual_form import submit_manual_form

        return await submit_manual_form(settings, actor=actor, workspace=workspace, text=text)

    return await record_free_text(settings, actor=actor, workspace=workspace, message=message)


async def _main_menu_reply(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, greeting: bool = False
) -> list[Reply]:
    status = await current_status(settings, actor=actor, workspace=workspace)
    title = "👋 Здравствуйте!" if greeting else f"📒 {workspace.name}"
    lines = [title]
    if greeting:
        lines.append(f"\n📒 Бюджет: {workspace.name}")
    lines.append(f"Период: {views.format_range(status.start_date, status.end_inclusive)}")
    lines.append("\nЧтобы записать трату, просто напишите её: «кофе 250».")
    return [Reply(text="\n".join(lines), buttons=main_menu())]


# Ожидания, в которые обычно вводится число или дата. Фраза «такси 700»
# в них — новая трата, а не ответ: ожидание снимается (G-13, G-16).
_VALUE_PENDING_KINDS = frozenset(
    {
        "category_limit",
        "draft_edit",
        "goal_allocate",
        "goal_use",
        "goal_release",
        "manual_form",
        "payment_new",
        "goal_new",
        "transaction_edit",
    }
)


async def _abandon_pending_for_new_entry(
    settings: Settings, *, actor: ActorContext, text: str
) -> bool:
    from fintracker.application.conversation.guards import (
        bare_date,
        looks_like_new_entry,
        single_amount,
    )
    from fintracker.application.conversation.pending import clear_pending, peek_pending

    if not looks_like_new_entry(text) or single_amount(text) or bare_date(text):
        return False
    pending = await peek_pending(settings, user_id=actor.user_id, workspace_id=actor.workspace_id)
    if pending is None or pending.kind not in _VALUE_PENDING_KINDS:
        return False
    step = str(pending.payload.get("step") or "")
    action = str(pending.payload.get("action") or "")
    # Названия и комментарии могут содержать цифры: «Отпуск 2027», «2 кофе».
    if pending.kind in {"goal_new", "payment_new"} and step in {"", "name"}:
        return False
    if pending.kind == "manual_form" and step == "comment":
        return False
    if pending.kind == "transaction_edit" and action == "note":
        return False
    await clear_pending(settings, user_id=actor.user_id, workspace_id=actor.workspace_id)
    return True


async def _continue_pending(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply] | None:
    """Применить ввод, обещанный нажатой кнопкой (FR-21, FR-45, G-13…G-16)."""
    from fintracker.application.conversation.pending import clear_pending, peek_pending

    text = (message.text or "").strip()
    if not text:
        return None
    pending = await peek_pending(settings, user_id=actor.user_id, workspace_id=actor.workspace_id)
    if pending is None:
        return None
    if pending.workspace_id is not None and pending.workspace_id != actor.workspace_id:
        await clear_pending(settings, user_id=actor.user_id, workspace_id=pending.workspace_id)
        return [
            Reply(
                text=(
                    "📒 Вы сменили бюджет\n\nНезавершённое действие осталось в "
                    "предыдущем бюджете и отменено. Повторите команду в текущем."
                )
            )
        ]

    from fintracker.application.conversation import category_flow, goals_flow, payments_flow

    clear_after = True
    match pending.kind:
        case "transaction_edit" | "transfer_account":
            from fintracker.application.conversation.transaction_flow import (
                account_input,
                edit_input,
            )

            handler = edit_input if pending.kind == "transaction_edit" else account_input
            replies = await handler(
                settings, actor=actor, workspace=workspace, pending=pending, text=text
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "category_rename":
            replies = await category_flow.apply_pending_rename(
                settings,
                actor=actor,
                workspace=workspace,
                category_id=uuid.UUID(str(pending.payload["category_id"])),
                name=text,
            )
        case "category_limit":
            replies = await category_flow.apply_pending_limit(
                settings,
                actor=actor,
                workspace=workspace,
                category_id=uuid.UUID(str(pending.payload["category_id"])),
                text=text,
                target_period_id=(
                    uuid.UUID(str(pending.payload["target_period_id"]))
                    if pending.payload.get("target_period_id")
                    else None
                ),
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "draft_edit":
            replies = await sections.apply_draft_edit(
                settings,
                actor=actor,
                workspace=workspace,
                draft_id=uuid.UUID(str(pending.payload["draft_id"])),
                candidate_id=(
                    uuid.UUID(str(pending.payload["candidate_id"]))
                    if pending.payload.get("candidate_id")
                    else None
                ),
                expected_version=pending.payload.get("candidate_version"),
                text=text,
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "goal_new":
            replies = await goals_flow.create_goal_from_text(
                settings,
                actor=actor,
                workspace=workspace,
                text=text,
                pending_payload=pending.payload,
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "goal_allocate" | "goal_use" | "goal_release":
            operation = pending.kind.removeprefix("goal_")
            replies = await goals_flow.apply_goal_amount(
                settings,
                actor=actor,
                workspace=workspace,
                goal_id=uuid.UUID(str(pending.payload["goal_id"])),
                operation=operation,
                text=text,
                idempotency_key=message.source_key,
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "payment_new":
            replies = await payments_flow.create_payment_from_text(
                settings,
                actor=actor,
                workspace=workspace,
                text=text,
                pending_payload=pending.payload,
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "manual_form":
            from fintracker.application.conversation.manual_form import continue_manual_form

            replies = await continue_manual_form(
                settings,
                actor=actor,
                workspace=workspace,
                text=text,
                payload=pending.payload,
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "workspace_delete":
            replies = await _confirm_workspace_delete(
                settings, actor=actor, workspace=workspace, text=text, message=message
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "category_new":
            replies = await category_flow.create_category_from_name(
                settings, actor=actor, workspace=workspace, name=text
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "profile_name":
            from fintracker.application.conversation.settings_flow import apply_profile_name

            replies = await apply_profile_name(
                settings, actor=actor, workspace=workspace, name=text
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "budget_rename":
            from fintracker.application.conversation.settings_flow import apply_budget_rename

            replies = await apply_budget_rename(
                settings, actor=actor, workspace=workspace, name=text
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "goal_edit":
            replies = await goals_flow.apply_goal_edit(
                settings, actor=actor, workspace=workspace, payload=pending.payload, text=text
            )
            clear_after = not any(reply.retry_input for reply in replies)
        case "occurrence_settle":
            # Ожидание оплаты сохраняется до подтверждения самой траты.
            from fintracker.application.conversation.pending import set_pending

            await set_pending(
                settings,
                user_id=actor.user_id,
                workspace_id=actor.workspace_id,
                kind="occurrence_settle",
                payload=pending.payload,
            )
            return None
        case _:
            replies = [Reply(text="🔄 Кнопка устарела.\n\nПовторите действие из меню.")]
    if clear_after:
        await clear_pending(settings, user_id=actor.user_id, workspace_id=actor.workspace_id)
    return replies


async def _confirm_workspace_delete(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    text: str,
    message: IncomingMessage,
) -> list[Reply]:
    """Удаление только после нажатой кнопки и точного названия (FR-83).

    Фраза «удалить …» вне этого шага не удаляет бюджет: иначе обычная просьба
    вроде «удалить последнюю трату» могла бы закрыть общий бюджет.
    """
    from fintracker.application.identity.membership import delete_workspace

    typed = text.strip()
    if typed.casefold().startswith("удалить "):
        typed = typed[len("удалить ") :].strip()
    if typed.strip("«»\"' ").casefold() != workspace.name.strip().casefold():
        return [
            Reply(
                text=(
                    "⚠️ Название не совпало\n\n"
                    f"Чтобы удалить бюджет, отправьте его название: {workspace.name}\n\n"
                    "Передумали — нажмите «Отмена»."
                ),
                buttons=((Button("✕ Отмена", callback("noop", "keepws")),),),
                retry_input=True,
            )
        ]
    await delete_workspace(
        settings,
        workspace_id=actor.require_workspace(),
        admin_user_id=actor.user_id,
        confirmation_name=workspace.name,
        correlation_id=message.correlation_id or uuid.uuid4().hex,
    )
    return [
        Reply(
            text=(
                f"🗑 Бюджет «{workspace.name}» удалён\n\n"
                "Участники потеряли к нему доступ. Остальные ваши бюджеты не изменились."
            ),
            buttons=start_menu(returning=True),
        )
    ]


async def _existing_message_reply(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    state: str | None,
    previous_text: str | None,
    message: IncomingMessage,
    extraction: ExtractionResult,
) -> list[Reply]:
    """Ответ на повтор или редакцию уже обработанного сообщения (R-01, R-03).

    Проведённая запись не дублируется: изменение текста предлагается как
    исправление связанной операции и требует подтверждения.
    """
    if state != "posted":
        return await sections.draft_reply(
            settings, actor=actor, workspace=workspace, draft_id=draft_id
        )

    new_text = (message.text or "").strip()
    if previous_text is not None and new_text and previous_text.strip() != new_text:
        transaction_id = await sections.posted_transaction_of_draft(
            settings, actor=actor, draft_id=draft_id
        )
        if transaction_id is not None and len(extraction.candidates) == 1:
            from fintracker.application.conversation.corrections import propose_edit_correction

            candidate = extraction.candidates[0]
            amount = (
                Money(candidate.amount_minor, candidate.currency or workspace.currency)
                if candidate.amount_minor is not None
                else None
            )
            proposal = await propose_edit_correction(
                settings,
                actor=actor,
                workspace=workspace,
                transaction_id=transaction_id,
                new_amount=amount,
                new_date=candidate.occurred_date,
            )
            if proposal is not None:
                return proposal
    return await sections.posted_draft_reply(
        settings, actor=actor, workspace=workspace, draft_id=draft_id
    )


async def record_free_text(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply]:
    """Полный путь свободного ввода: извлечение → черновик → карточка.

    Три стадии с короткими транзакциями: подготовка, вызов модели вне
    транзакции базы и сохранение результата. Долгий ответ провайдера не
    удерживает соединение и не рвёт транзакцию (AUD-07).
    Повтор обработки того же входящего события находит прежний черновик и не
    создаёт вторую запись (AUD-02).
    """
    assert message.text is not None
    workspace_id = actor.require_workspace()
    local_date = message.received_at.astimezone(ZoneInfo(workspace.timezone)).date()

    # --- Стадия 1: короткая транзакция подготовки -------------------------
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        known_draft_id: uuid.UUID | None = None
        known_state: str | None = None
        known_text: str | None = None
        source_key = message.source_key
        if source_key is not None:
            existing = await find_message_draft(
                session,
                workspace_id=workspace_id,
                owner_user_id=actor.user_id,
                source_message_key=source_key,
            )
            if existing is not None:
                known_draft_id = existing.id
                known_state = existing.state
                known_text = existing.raw_text

        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == actor.user_id,
                )
            )
        ).scalar_one()
        autopost = membership.autopost_enabled
        assume_self = membership.assume_self_spender
        large_threshold = membership.large_amount_threshold_minor

        # Защитные намерения распознаются детерминированно и до вызова модели:
        # вопрос, гипотеза и отрицание не проводятся как покупка независимо от
        # доступности AI (FR-12, AI-05, NFR-14).
        extraction = await extract_from_text(
            session,
            settings=settings,
            actor=actor,
            text=message.text,
            workspace_currency=workspace.currency,
            reference_date=local_date,
            assume_self_spender=assume_self,
        )
        catalog = None
        if extraction.intent is Intent.RECORD_TRANSACTION and settings.ai.enabled:
            from fintracker.application.intelligence.extraction import load_catalog

            catalog = await load_catalog(
                session,
                workspace_id=workspace_id,
                currency=workspace.currency,
                timezone=workspace.timezone,
            )

    if known_draft_id is not None:
        # Повтор или редакция того же сообщения: вторая независимая трата не
        # создаётся (R-01, R-03).
        return await _existing_message_reply(
            settings,
            actor=actor,
            workspace=workspace,
            draft_id=known_draft_id,
            state=known_state,
            previous_text=known_text,
            message=message,
            extraction=extraction,
        )

    guard_reply = _guard_reply(extraction.intent)
    if guard_reply is not None:
        return guard_reply
    if extraction.intent is Intent.REMINDER:
        from fintracker.application.conversation.payments_flow import (
            start_payment_from_reminder,
        )

        return await start_payment_from_reminder(
            settings, actor=actor, workspace=workspace, text=message.text
        )
    if extraction.intent is Intent.QUESTION:
        from fintracker.application.conversation.analytics_flow import answer_question

        return await answer_question(
            settings, actor=actor, workspace=workspace, question=message.text
        )
    if extraction.intent is Intent.CREATE_CATEGORY:
        from fintracker.application.conversation.category_flow import create_category_from_text

        return await create_category_from_text(
            settings, actor=actor, workspace=workspace, text=message.text
        )

    # --- Стадия 2: вызов модели вне транзакции базы -----------------------
    if catalog is not None:
        extraction = await _extract_with_model_or_fallback(
            settings,
            actor=actor,
            workspace=workspace,
            text=message.text,
            reference_date=local_date,
            deterministic=extraction,
            catalog=catalog,
        )
        guard_reply = _guard_reply(extraction.intent)
        if guard_reply is not None:
            return guard_reply

    if not extraction.candidates:
        return not_understood_reply(message.text)
    if not workspace.currency:  # pragma: no cover - защита контракта
        raise ValidationFailed("У бюджета не задана валюта")

    # --- Стадия 3: короткая транзакция сохранения -------------------------
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        conflicting: DraftAlreadyExists | None = None
        try:
            draft, candidates = await create_draft_with_candidates(
                session,
                settings=settings,
                actor=actor,
                source_kind="text",
                raw_text=message.text,
                extraction=extraction,
                logical_message_id=message.inbound_event_id,
                source_message_key=source_key,
            )
        except DraftAlreadyExists as clash:
            # Параллельное исполнение того же входа: результат уже существует.
            conflicting = clash
        else:
            draft_id = draft.id
            from fintracker.application.conversation.pending import peek_pending, set_pending

            pending = await peek_pending(
                settings, user_id=actor.user_id, workspace_id=actor.workspace_id
            )
            if (
                pending is not None
                and pending.kind == "occurrence_settle"
                and not pending.payload.get("draft_id")
            ):
                await set_pending(
                    settings,
                    user_id=actor.user_id,
                    workspace_id=actor.workspace_id,
                    kind="occurrence_settle",
                    payload={**pending.payload, "draft_id": str(draft_id)},
                )
            candidate_fields = [
                CandidateFields.from_payload(dict(row.fields)) for row in candidates
            ]

    if conflicting is not None:
        return await _existing_message_reply(
            settings,
            actor=actor,
            workspace=workspace,
            draft_id=conflicting.draft_id,
            state=conflicting.state,
            previous_text=message.text,
            message=message,
            extraction=extraction,
        )

    if extraction.question or not _autopost_allowed(
        candidate_fields, autopost=autopost, large_threshold=large_threshold
    ):
        return await sections.draft_reply(
            settings, actor=actor, workspace=workspace, draft_id=draft_id
        )

    return await sections.confirm_draft(
        settings, actor=actor, workspace=workspace, draft_id=draft_id, origin="telegram_text"
    )


async def _extract_with_model_or_fallback(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    text: str,
    reference_date: dt.date,
    deterministic: ExtractionResult,
    catalog: Any,
) -> ExtractionResult:
    """Разбор моделью с деградацией на детерминированный путь (NFR-14, A103).

    Черновик попытки сохраняется короткой транзакцией, вызов провайдера идёт
    вне транзакции базы (AUD-07). При недоступности модели или исчерпанной
    квоте используется результат детерминированного разбора.
    """
    from fintracker.application.conversation.entry import create_draft_with_candidates
    from fintracker.application.intelligence.extraction import extract_with_model

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        draft, _ = await create_draft_with_candidates(
            session,
            settings=settings,
            actor=actor,
            source_kind="text",
            raw_text=text,
            extraction=ExtractionResult(intent=Intent.UNKNOWN, candidates=[]),
        )
        draft_id = draft.id
        draft_version = draft.version

    try:
        return await extract_with_model(
            settings,
            actor=actor,
            draft_id=draft_id,
            draft_version=draft_version,
            text=text,
            catalog=catalog,
            reference_date=reference_date,
        )
    except (ProviderUnavailable, QuotaExceeded, ValidationFailed) as exc:
        logger.info("ai_fallback_to_deterministic", reason=type(exc).__name__)
        return deterministic
    finally:
        # Технический черновик попытки не остаётся активным (RET-03).
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            from fintracker.db.models.platform import Draft

            await session.execute(
                update(Draft)
                .where(Draft.id == draft_id, Draft.state.in_(("received", "processing")))
                .values(state="cancelled")
            )


def _guard_reply(intent: Intent) -> list[Reply] | None:
    """Ответы на намерения, которые нельзя проводить как расход (FR-12)."""
    match intent:
        case Intent.HYPOTHETICAL:
            return [
                Reply(
                    text=(
                        "💭 Пока это только план\n\nНичего не записано. В «Бюджете» видно, "
                        "сколько осталось по категориям."
                    ),
                    buttons=((Button("📒 Бюджет", callback("menu", "budget")),),),
                )
            ]
        case Intent.NEGATED:
            return [
                Reply(
                    text="👌 Понял, покупки не было — ничего не записано.",
                    buttons=((Button("🏠 Меню", callback("menu", "main")),),),
                )
            ]
        case Intent.CHANGE_LIMIT:
            return [
                Reply(
                    text=(
                        "💰 Лимиты меняются в разделе «Категории»\n\nОткройте «⚙️ Управление», "
                        "выберите категорию и нажмите «💰 Лимит»."
                    ),
                    buttons=((Button("🗂 Категории", callback("cat", "manage")),),),
                )
            ]
        case _:
            return None


def _autopost_allowed(
    candidates: Sequence[CandidateFields], *, autopost: bool, large_threshold: int | None
) -> bool:
    """Правила автозаписи (FR-19).

    Автоматически записываются только однозначные обычные расходы и доходы
    из текста после включения автозаписи самим отправителем.
    """
    if not autopost:
        return False
    if len(candidates) != 1:
        # Неоднозначные пакеты подтверждаются (FR-19).
        return False
    candidate = candidates[0]
    if not candidate.is_complete:
        return False
    if candidate.kind not in {"expense", "income"}:
        # Переводы, займы и сложные разделения подтверждаются.
        return False
    if candidate.category_id is None:
        return False
    # «Крупная сумма» — настраиваемый порог, а не оценка AI (FR-19).
    is_large = large_threshold is not None and (candidate.amount_minor or 0) >= large_threshold
    return not is_large
