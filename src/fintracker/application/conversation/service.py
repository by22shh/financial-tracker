"""Маршрутизатор пользовательских путей (раздел 6 ТЗ, NFR-02, NFR-03).

Один сервис обслуживает Telegram, ручной ввод и будущий Mini App: бизнес-правила
не дублируются в интерфейсе (ADR-10).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from fintracker.application.conversation import sections
from fintracker.application.conversation.context import (
    HELP_TEXT,
    active_context,
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
from fintracker.core.money import Money
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
            names = "\n".join(
                f"• {item.name} ({item.role.value}, ID {item.short_id})" for item in budgets
            )
            text = f"С возвращением! Ваши бюджеты:\n{names}"
            if unfinished:
                text += "\nЕсть незавершённая настройка бюджета."
            return [
                Reply(
                    text=text,
                    buttons=start_menu(returning=True, unfinished=unfinished),
                )
            ]
        if unfinished:
            return [
                Reply(
                    text=(
                        "С возвращением! Настройка бюджета не завершена — "
                        "можно продолжить с того же шага."
                    ),
                    buttons=start_menu(returning=False, unfinished=True),
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

    # Кнопка, обещавшая продолжение, получает следующее сообщение (G-13…G-16).
    continued = await _continue_pending(settings, actor=actor, workspace=workspace, message=message)
    if continued is not None:
        return continued

    from fintracker.application.conversation.clarify import try_answer_open_question

    answered = await try_answer_open_question(
        settings, actor=actor, workspace=workspace, message=message
    )
    if answered is not None:
        return answered

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


async def _continue_pending(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply] | None:
    """Применить ввод, обещанный нажатой кнопкой (FR-21, FR-45, G-13…G-16)."""
    from fintracker.application.conversation.pending import take_pending

    text = (message.text or "").strip()
    if not text:
        return None
    pending = await take_pending(settings, user_id=actor.user_id, workspace_id=actor.workspace_id)
    if pending is None:
        return None

    from fintracker.application.conversation import category_flow, goals_flow, payments_flow

    match pending.kind:
        case "category_rename":
            return await category_flow.apply_pending_rename(
                settings,
                actor=actor,
                workspace=workspace,
                category_id=uuid.UUID(str(pending.payload["category_id"])),
                name=text,
            )
        case "category_limit":
            return await category_flow.apply_pending_limit(
                settings,
                actor=actor,
                workspace=workspace,
                category_id=uuid.UUID(str(pending.payload["category_id"])),
                text=text,
            )
        case "goal_new":
            return await goals_flow.create_goal_from_text(
                settings, actor=actor, workspace=workspace, text=text
            )
        case "payment_new":
            return await payments_flow.create_payment_from_text(
                settings, actor=actor, workspace=workspace, text=text
            )
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
    return None


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
        return [Reply(text="Не понял сообщение. Отправьте /help, чтобы увидеть примеры.")]
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
