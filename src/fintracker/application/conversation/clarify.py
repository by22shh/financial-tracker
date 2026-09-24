"""Ответы на открытые уточняющие вопросы (AR-06, R07, AI-05).

Свободный короткий ответ не попадает в чужой вопрос наугад: при нескольких
открытых вопросах требуется явный выбор, а закрытый вопрос не изменяет
другой черновик.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import IncomingMessage, Reply
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import NotFound
from fintracker.db.models.access import Workspace
from fintracker.db.models.platform import Candidate, Clarification, Draft
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

# Короткий ответ: одно число без иных слов о покупке.
_BARE_AMOUNT = re.compile(r"^\s*(\d[\d\s.,]*)\s*(?:₽|руб\.?|р\.?)?\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    clarification_id: uuid.UUID
    draft_id: uuid.UUID
    candidate_id: uuid.UUID | None
    field: str
    question: str
    raw_text: str | None


async def open_questions(settings: Settings, *, actor: ActorContext) -> list[OpenQuestion]:
    """Открытые вопросы этого участника в текущем бюджете (R07)."""
    workspace_id = actor.require_workspace()
    now = dt.datetime.now(dt.UTC)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            await session.execute(
                select(Clarification, Draft.raw_text)
                .join(
                    Draft,
                    (Draft.workspace_id == Clarification.workspace_id)
                    & (Draft.id == Clarification.draft_id),
                )
                .where(
                    Clarification.workspace_id == workspace_id,
                    Clarification.state == "open",
                    Clarification.expires_at > now,
                    Draft.owner_user_id == actor.user_id,
                    Draft.state.in_(("received", "processing", "needs_clarification")),
                )
                .order_by(Clarification.created_at)
            )
        ).all()
    return [
        OpenQuestion(
            clarification_id=row[0].id,
            draft_id=row[0].draft_id,
            candidate_id=row[0].candidate_id,
            field=row[0].field,
            question=row[0].question,
            raw_text=row[1],
        )
        for row in rows
    ]


async def try_answer_open_question(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    message: IncomingMessage,
) -> list[Reply] | None:
    """Если текст похож на ответ на открытый вопрос — обработать (AR-06)."""
    text = (message.text or "").strip()
    if not _BARE_AMOUNT.match(text):
        return None
    questions = [
        item for item in await open_questions(settings, actor=actor) if item.field == "amount"
    ]
    if not questions:
        return None
    if len(questions) > 1:
        # Ни один вопрос не закрывается наугад: нужен явный выбор (AR-06).
        lines = ["Открыто несколько вопросов. К какому относится ответ?"]
        rows: list[tuple[Button, ...]] = []
        for index, item in enumerate(questions[:4], start=1):
            source = (item.raw_text or item.question)[:40]
            lines.append(f"{index}. {source}")
            rows.append(
                (
                    Button(
                        f"{index}. {source}"[:40],
                        callback("clr", "pick", short(item.clarification_id), text[:12]),
                    ),
                )
            )
        rows.append((Button("Это новая трата", callback("clr", "new")),))
        return [Reply(text="\n".join(lines), buttons=tuple(rows))]

    return await answer_amount(
        settings,
        actor=actor,
        workspace=workspace,
        clarification_id=questions[0].clarification_id,
        raw_amount=text,
    )


async def answer_amount(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    clarification_id: uuid.UUID,
    raw_amount: str,
) -> list[Reply]:
    """Подставить сумму в кандидата открытого вопроса (R07)."""
    from decimal import Decimal

    from fintracker.core.money import Money
    from fintracker.domain.parsing.amounts import parse_amounts

    amounts = parse_amounts(raw_amount)
    if not amounts:
        return [Reply(text="✍️ Не удалось разобрать сумму\n\nОтправьте число, например 500.")]
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        row = (
            await session.execute(
                select(Clarification).where(
                    Clarification.workspace_id == workspace_id,
                    Clarification.id == clarification_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("Вопрос недоступен")
        if row.state != "open":
            # Закрытый вопрос не изменяет другой черновик (AR-06).
            return [
                Reply(
                    text=(
                        "ℹ️ Этот вопрос уже закрыт.\n\nОтправьте трату отдельным "
                        "сообщением, если нужно записать новую."
                    )
                )
            ]
        draft = (
            await session.execute(
                select(Draft).where(
                    Draft.workspace_id == workspace_id,
                    Draft.id == row.draft_id,
                    Draft.owner_user_id == actor.user_id,
                )
            )
        ).scalar_one_or_none()
        if draft is None or draft.state in {"cancelled", "expired", "posted"}:
            return [Reply(text="ℹ️ Черновик больше не активен.")]

        candidate = (
            await session.execute(
                select(Candidate).where(
                    Candidate.workspace_id == workspace_id,
                    Candidate.id == row.candidate_id,
                )
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise NotFound("Кандидат недоступен")

        amount = Money.from_decimal(Decimal(amounts[0].value), workspace.currency)
        fields = dict(candidate.fields)
        fields["amount_minor"] = amount.minor
        fields["currency"] = amount.currency
        candidate.fields = fields
        candidate.ambiguities = [
            item for item in candidate.ambiguities if item.get("field") != "amount"
        ]
        candidate.state = "needs_clarification" if candidate.ambiguities else "ready"
        candidate.version += 1
        row.state = "answered"
        row.answered_at = dt.datetime.now(dt.UTC)
        still_open = (
            (
                await session.execute(
                    select(Clarification.id).where(
                        Clarification.workspace_id == workspace_id,
                        Clarification.draft_id == draft.id,
                        Clarification.state == "open",
                    )
                )
            )
            .scalars()
            .all()
        )
        draft.state = "needs_clarification" if still_open else "ready"
        draft.version += 1
        draft_id = draft.id

    from fintracker.application.conversation.sections import draft_reply

    return await draft_reply(settings, actor=actor, workspace=workspace, draft_id=draft_id)


async def clarify_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Кнопки выбора вопроса (AR-06)."""
    if action == "new":
        return [
            Reply(
                text=(
                    "👌 Хорошо\n\nОтправьте новую трату отдельным сообщением с "
                    "названием и суммой, например «продукты 500»."
                )
            )
        ]
    if action in _CHOICES and rest:
        from fintracker.application.conversation.callbacks import _resolve_uuid

        draft_id = await _resolve_uuid(
            settings,
            workspace_id=actor.require_workspace(),
            user_id=actor.user_id,
            table="drafts",
            prefix=rest[0],
        )
        return await resolve_choice(
            settings, actor=actor, workspace=workspace, draft_id=draft_id, choice=action
        )
    if action == "pick" and len(rest) >= 2:
        questions = await open_questions(settings, actor=actor)
        target = next((item for item in questions if short(item.clarification_id) == rest[0]), None)
        if target is None:
            return [Reply(text="ℹ️ Этот вопрос уже закрыт.")]
        return await answer_amount(
            settings,
            actor=actor,
            workspace=workspace,
            clarification_id=target.clarification_id,
            raw_amount=rest[1],
        )
    return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]


# Ответы кнопками на открытые вопросы черновика и поля, которые они закрывают.
_CHOICES: dict[str, frozenset[str]] = {
    "person": frozenset({"spender_person_id"}),
    "noperson": frozenset({"spender_person_id"}),
    "today": frozenset({"date", "occurred_date"}),
    "plan": frozenset({"date", "occurred_date"}),
    "noteall": frozenset({"note"}),
    "nonote": frozenset({"note"}),
}


async def resolve_choice(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    draft_id: uuid.UUID,
    choice: str,
) -> list[Reply]:
    """Закрыть открытый вопрос выбранным вариантом и показать черновик."""
    from zoneinfo import ZoneInfo

    from fintracker.application.catalog.directory import create_person
    from fintracker.application.conversation.entry import (
        CandidateFields,
        _extract_note,
        load_draft,
    )
    from fintracker.application.conversation.sections import draft_reply

    fields_closed = _CHOICES[choice]
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    notice: str | None = None
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        draft, candidates = await load_draft(
            session, workspace_id=workspace_id, draft_id=draft_id, owner_id=actor.user_id
        )
        if draft.state in {"posted", "cancelled", "expired"}:
            return [Reply(text="ℹ️ Эта запись уже сохранена или отменена.")]
        if choice == "plan":
            draft.state = "cancelled"
            draft.version += 1
            for candidate in candidates:
                if candidate.state != "posted":
                    candidate.state = "cancelled"
            plan_only = True
        else:
            plan_only = False
            common_note = _extract_note(draft.raw_text or "")[1] if choice == "noteall" else None
            for candidate in candidates:
                if candidate.state in {"excluded", "cancelled", "posted"}:
                    continue
                ambiguities = list(candidate.ambiguities or [])
                matched = [item for item in ambiguities if item.get("field") in fields_closed]
                if not matched:
                    continue
                fields = CandidateFields.from_payload(dict(candidate.fields))
                if choice == "person":
                    name = str(matched[0].get("value") or "").strip()
                    if name:
                        person = await create_person(session, uow, actor=actor, name=name)
                        fields.spender_person_id = person.id
                        notice = f"👤 Человек «{person.name}» добавлен в бюджет."
                elif choice == "today":
                    fields.occurred_date = today
                    fields.date_expression = None
                elif choice == "noteall" and common_note:
                    fields.note = common_note
                candidate.fields = fields.to_payload()
                candidate.ambiguities = [
                    item for item in ambiguities if item.get("field") not in fields_closed
                ]
                candidate.state = "needs_clarification" if candidate.ambiguities else "ready"
                candidate.version += 1
            open_rows = (
                (
                    await session.execute(
                        select(Clarification).where(
                            Clarification.workspace_id == workspace_id,
                            Clarification.draft_id == draft_id,
                            Clarification.state == "open",
                        )
                    )
                )
                .scalars()
                .all()
            )
            now = dt.datetime.now(dt.UTC)
            for row in open_rows:
                if row.field in fields_closed:
                    row.state = "answered"
                    row.answered_at = now
            still_open = any(
                row.state == "open" and row.field not in fields_closed for row in open_rows
            )
            draft.state = "needs_clarification" if still_open else "ready"
            draft.version += 1
    if plan_only:
        return [
            Reply(
                text=(
                    "💭 Хорошо, это план\n\nНичего не записано. Регулярный платёж можно "
                    "добавить в «Платежи» — бот напомнит о сроке."
                ),
                buttons=(
                    (
                        Button("🗓 Добавить платёж", callback("pay", "new")),
                        Button("🏠 Меню", callback("menu", "main")),
                    ),
                ),
            )
        ]
    return await draft_reply(
        settings, actor=actor, workspace=workspace, draft_id=draft_id, notice=notice
    )
