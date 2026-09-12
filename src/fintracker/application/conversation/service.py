"""Маршрутизатор пользовательских путей (раздел 6 ТЗ).

Один сервис обслуживает Telegram, ручной ввод и будущий Mini App: бизнес-правила
не дублируются в интерфейсе (ADR-10).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.conversation import sections
from fintracker.application.conversation.context import (
    HELP_TEXT,
    active_context,
    load_actor,
    no_budget_reply,
)
from fintracker.application.conversation.entry import (
    CandidateFields,
    ExtractionResult,
    create_draft_with_candidates,
    extract_from_text,
)
from fintracker.application.conversation.keyboards import (
    Button,
    callback,
    confirm_candidate,
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
    ValidationFailed,
)
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Membership, Workspace
from fintracker.db.models.platform import Candidate, Draft
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.domain.parsing.intent import Intent

logger = get_logger("conversation")


async def handle(settings: Settings, message: IncomingMessage) -> list[Reply]:
    """Обработать входящее сообщение и вернуть ответы пользователю."""
    try:
        return await _route(settings, message)
    except DomainError as exc:
        logger.info(
            "conversation_domain_error",
            code=exc.code.value,
            correlation_id=message.correlation_id,
        )
        return [Reply(text=exc.message)]


async def _route(settings: Settings, message: IncomingMessage) -> list[Reply]:
    async with session_scope(settings, RuntimeRole.API) as session:
        user = await ensure_user(session, telegram_user_id=message.telegram_user_id)
        user_id = user.id

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

    return [Reply(text="Не понял сообщение. Отправьте /help, чтобы увидеть возможности.")]


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
        async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
            budgets = await list_budgets(session, user_id)
        if budgets:
            names = "\n".join(
                f"• {item.name} ({item.role.value}, ID {item.short_id})" for item in budgets
            )
            return [
                Reply(
                    text=f"С возвращением! Ваши бюджеты:\n{names}",
                    buttons=start_menu(returning=True),
                )
            ]
        return [
            Reply(
                text=(
                    "Добро пожаловать! Здесь можно вести личный или общий бюджет.\n\n"
                    "Если бюджет уже создан другим человеком, попросите у него код "
                    "приглашения."
                ),
                buttons=start_menu(returning=False),
            )
        ]

    if command == "/help":
        return [Reply(text=HELP_TEXT, buttons=main_menu())]

    if command == "/join":
        if argument:
            return await submit_join_code(
                settings, user_id=user_id, raw_code=argument, message=message
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
            return await sections.history_view(settings, actor=actor, workspace=workspace)
        case "/report":
            from fintracker.application.conversation.analytics_flow import report_view

            return await report_view(settings, actor=actor, workspace=workspace)
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
        case "/add":
            from fintracker.application.conversation.manual_form import start_manual_form

            return await start_manual_form(settings, actor=actor, workspace=workspace)
        case _:
            return [Reply(text="Неизвестная команда. Отправьте /help.")]


async def _cancel_active(
    settings: Settings, *, user_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> list[Reply]:
    """`/cancel` отменяет активный диалог или черновик, но не проведённую запись."""
    if workspace_id is None:
        return [Reply(text="Отменять нечего.")]
    async with session_scope(
        settings, RuntimeRole.API, user_id=user_id, workspace_id=workspace_id
    ) as session:
        drafts = (
            (
                await session.execute(
                    select(Draft).where(
                        Draft.workspace_id == workspace_id,
                        Draft.owner_user_id == user_id,
                        Draft.state.in_(("received", "processing", "needs_clarification", "ready")),
                    )
                )
            )
            .scalars()
            .all()
        )
        for draft in drafts:
            draft.state = "cancelled"
            draft.version += 1
        if drafts:
            from sqlalchemy import update

            await session.execute(
                update(Candidate)
                .where(
                    Candidate.workspace_id == workspace_id,
                    Candidate.draft_id.in_([draft.id for draft in drafts]),
                    Candidate.state != "posted",
                )
                .values(state="cancelled")
            )
    if not drafts:
        return [Reply(text="Активных незавершённых записей нет.")]
    return [
        Reply(
            text=(
                f"Отменено незавершённых записей: {len(drafts)}. "
                "Уже проведённые операции не затронуты."
            )
        )
    ]


async def _handle_free_text(
    settings: Settings, message: IncomingMessage, user_id: uuid.UUID
) -> list[Reply]:
    from fintracker.application.conversation.onboarding_flow import (
        continue_wizard_input,
        has_active_wizard,
    )

    assert message.text is not None

    if await has_active_wizard(settings, user_id=user_id):
        handled = await continue_wizard_input(settings, user_id=user_id, message=message)
        if handled is not None:
            return handled

    workspace_id = await active_context(settings, user_id=user_id, message=message)
    if workspace_id is None:
        return no_budget_reply()

    actor, workspace = await load_actor(
        settings,
        user_id=user_id,
        workspace_id=workspace_id,
        correlation_id=message.correlation_id,
    )
    if message.text.strip().lower().startswith("удалить "):
        from fintracker.application.identity.membership import delete_workspace

        confirmation = message.text.strip()[len("удалить ") :].strip()
        await delete_workspace(
            settings,
            workspace_id=workspace_id,
            admin_user_id=actor.user_id,
            confirmation_name=confirmation,
            correlation_id=message.correlation_id or uuid.uuid4().hex,
        )
        return [Reply(text=f"Бюджет «{workspace.name}» удалён.")]

    from fintracker.application.conversation.corrections import try_handle_correction

    corrected = await try_handle_correction(
        settings, actor=actor, workspace=workspace, message=message
    )
    if corrected is not None:
        return corrected

    if "|" in message.text:
        from fintracker.application.conversation.manual_form import submit_manual_form

        return await submit_manual_form(
            settings, actor=actor, workspace=workspace, text=message.text
        )

    return await record_free_text(settings, actor=actor, workspace=workspace, message=message)


async def record_free_text(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply]:
    """Полный путь свободного ввода: извлечение → черновик → карточка."""
    assert message.text is not None
    workspace_id = actor.require_workspace()
    local_date = message.received_at.astimezone(ZoneInfo(workspace.timezone)).date()

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
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

        guard_reply = _guard_reply(extraction.intent)
        if guard_reply is not None:
            return guard_reply
        if extraction.intent is Intent.QUESTION:
            from fintracker.application.conversation.analytics_flow import answer_question

            return await answer_question(
                settings, actor=actor, workspace=workspace, question=message.text
            )
        if extraction.intent is Intent.CREATE_CATEGORY:
            from fintracker.application.conversation.category_flow import (
                create_category_from_text,
            )

            return await create_category_from_text(
                settings, actor=actor, workspace=workspace, text=message.text
            )
        if extraction.intent is Intent.RECORD_TRANSACTION and settings.ai.enabled:
            # Модель уточняет свободную формулировку; сервер проверяет результат.
            extraction = await _extract_with_model_or_fallback(
                settings,
                session=session,
                actor=actor,
                workspace=workspace,
                text=message.text,
                reference_date=local_date,
                deterministic=extraction,
            )
            guard_reply = _guard_reply(extraction.intent)
            if guard_reply is not None:
                return guard_reply
        if not extraction.candidates:
            return [Reply(text="Не понял сообщение. Отправьте /help, чтобы увидеть примеры.")]
        if not workspace.currency:  # pragma: no cover - защита контракта
            raise ValidationFailed("У бюджета не задана валюта")

        draft, candidates = await create_draft_with_candidates(
            session,
            settings=settings,
            actor=actor,
            source_kind="text",
            raw_text=message.text,
            extraction=extraction,
        )
        draft_id = draft.id
        candidate_fields = [CandidateFields.from_payload(dict(row.fields)) for row in candidates]

    if extraction.question:
        summary = sections.draft_summary(candidate_fields, workspace.currency)
        return [
            Reply(
                text=f"{extraction.question}\n\nЧто уже распознано:\n{summary}",
                buttons=confirm_candidate(draft_id),
            )
        ]

    if not _autopost_allowed(candidate_fields, autopost=autopost, large_threshold=large_threshold):
        summary = sections.draft_summary(candidate_fields, workspace.currency)
        return [
            Reply(
                text=f"Проверьте запись перед сохранением:\n{summary}",
                buttons=confirm_candidate(draft_id),
            )
        ]

    return await sections.confirm_draft(
        settings, actor=actor, workspace=workspace, draft_id=draft_id, origin="telegram_text"
    )


async def _extract_with_model_or_fallback(
    settings: Settings,
    *,
    session: Any,
    actor: ActorContext,
    workspace: Workspace,
    text: str,
    reference_date: dt.date,
    deterministic: ExtractionResult,
) -> ExtractionResult:
    """Разбор моделью с деградацией на детерминированный путь (NFR-14, A103).

    При недоступности модели или исчерпанной квоте сохраняется входящий
    материал и используется результат детерминированного разбора.
    """
    from fintracker.application.conversation.entry import create_draft_with_candidates
    from fintracker.application.intelligence.extraction import (
        extract_with_model,
        load_catalog,
    )

    workspace_id = actor.require_workspace()
    catalog = await load_catalog(
        session,
        workspace_id=workspace_id,
        currency=workspace.currency,
        timezone=workspace.timezone,
    )
    draft, _ = await create_draft_with_candidates(
        session,
        settings=settings,
        actor=actor,
        source_kind="text",
        raw_text=text,
        extraction=ExtractionResult(intent=Intent.UNKNOWN, candidates=[]),
    )
    try:
        return await extract_with_model(
            settings,
            actor=actor,
            draft_id=draft.id,
            draft_version=draft.version,
            text=text,
            catalog=catalog,
            reference_date=reference_date,
        )
    except (ProviderUnavailable, QuotaExceeded, ValidationFailed) as exc:
        logger.info("ai_fallback_to_deterministic", reason=type(exc).__name__)
        return deterministic


def _guard_reply(intent: Intent) -> list[Reply] | None:
    """Ответы на намерения, которые нельзя проводить как расход (FR-12)."""
    match intent:
        case Intent.HYPOTHETICAL:
            return [
                Reply(
                    text=(
                        "Это сценарий, а не совершённая покупка — расход не записан.\n"
                        "Откройте «Бюджет», чтобы увидеть остаток по статье."
                    ),
                    buttons=((Button("Бюджет", callback("menu", "budget")),),),
                )
            ]
        case Intent.NEGATED:
            return [Reply(text="Понял, покупка не состоялась — ничего не записал.")]
        case Intent.REMINDER:
            return [
                Reply(
                    text=(
                        "Добавить напоминание об этом платеже?\n"
                        "Заметка сама по себе не создаёт платёж или автоматизацию."
                    ),
                    buttons=(
                        (
                            Button("Создать платёж", callback("pay", "new")),
                            Button("Не нужно", callback("noop", "x")),
                        ),
                    ),
                )
            ]
        case Intent.CHANGE_LIMIT:
            return [
                Reply(
                    text=(
                        "Изменение лимита проходит через карточку подтверждения. "
                        "Откройте «Категории» и выберите статью."
                    ),
                    buttons=((Button("Категории", callback("menu", "categories")),),),
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
