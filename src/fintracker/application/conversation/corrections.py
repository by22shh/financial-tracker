"""Исправление и отмена операций текстом и кнопками (FR-33, FR-34, FR-87)."""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.conversation.keyboards import Button, callback
from fintracker.application.conversation.types import IncomingMessage, Reply
from fintracker.application.ledger.service import (
    load_current_spec,
    restore_transaction,
    revise_transaction,
    void_transaction,
)
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ConflictError, ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.ledger import Transaction, TransactionRevision
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.parsing.amounts import parse_amounts
from fintracker.domain.parsing.dates import resolve_date_expression
from fintracker.domain.parsing.intent import Intent, classify_intent

# «Здесь было 800, а не 1800» — исправление, а не новая покупка (FR-12).
_AMOUNT_CORRECTION = re.compile(
    r"(?:здесь\s+)?было\s+(?P<new>\d[\d\s.,]*)\s*,?\s*а\s+не\s+(?P<old>\d[\d\s.,]*)",
    re.IGNORECASE,
)
_SIMPLE_CORRECTION = re.compile(
    r"испр(?:авь|ави)\s+(?P<old>\d[\d\s.,]*)\s+на\s+(?P<new>\d[\d\s.,]*)", re.IGNORECASE
)
_NOTE_ADD = re.compile(r"добавь\s+комментарий\s*[:\-—]?\s*(?P<note>.+)$", re.IGNORECASE | re.DOTALL)


async def try_handle_correction(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, message: IncomingMessage
) -> list[Reply] | None:
    """Если текст является исправлением — обработать, иначе вернуть None."""
    text = (message.text or "").strip()
    if not text:
        return None
    intent = classify_intent(text).intent
    if intent not in {
        Intent.CORRECT_TRANSACTION,
        Intent.CANCEL_TRANSACTION,
        Intent.ADD_NOTE,
    }:
        return None

    target = await _resolve_target(settings, actor=actor, message=message, text=text)
    if target is None:
        return [
            Reply(
                text=(
                    "Не понял, какую запись исправить. Ответьте на её карточку "
                    "или откройте историю и выберите операцию."
                ),
                buttons=((Button("История", callback("menu", "history")),),),
            )
        ]
    if isinstance(target, list):
        return target

    if intent is Intent.CANCEL_TRANSACTION:
        return await _propose_void(settings, actor=actor, workspace=workspace, target=target)
    if intent is Intent.ADD_NOTE:
        match = _NOTE_ADD.search(text)
        if match is None:
            return [Reply(text="Напишите «Добавь комментарий: текст».")]
        return await apply_note(
            settings,
            actor=actor,
            workspace=workspace,
            transaction_id=target,
            note=match.group("note").strip(),
            mode="append",
        )
    return await _propose_amount_or_date(
        settings, actor=actor, workspace=workspace, transaction_id=target, text=text
    )


async def _resolve_target(
    settings: Settings, *, actor: ActorContext, message: IncomingMessage, text: str
) -> uuid.UUID | list[Reply] | None:
    """Найти операцию для исправления (FR-33, A125).

    «Исправь последнюю трату» означает последнюю активную трату, добавленную
    самим отправителем в этом бюджете. При нескольких подходящих записях
    предлагается выбор, ни одна не исправляется наугад.
    """
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        if message.reply_to_message_id is not None:
            from fintracker.db.models.platform import NotificationDelivery, OutboxEvent

            row = (
                await session.execute(
                    select(OutboxEvent.aggregate_id)
                    .join(
                        NotificationDelivery,
                        NotificationDelivery.event_id == OutboxEvent.id,
                    )
                    .where(
                        NotificationDelivery.telegram_message_id == message.reply_to_message_id,
                        NotificationDelivery.recipient_user_id == actor.user_id,
                        OutboxEvent.aggregate_type == "transaction",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                return row

        amounts = parse_amounts(text)
        if amounts:
            targets: list[uuid.UUID] = []
            for amount in amounts:
                minor = Money.from_decimal(Decimal(amount.value), "RUB").minor
                rows = (
                    (
                        await session.execute(
                            select(Transaction.id)
                            .join(
                                TransactionRevision,
                                (TransactionRevision.workspace_id == Transaction.workspace_id)
                                & (TransactionRevision.transaction_id == Transaction.id)
                                & (TransactionRevision.revision == Transaction.current_revision),
                            )
                            .where(
                                Transaction.workspace_id == workspace_id,
                                Transaction.status == "posted",
                                TransactionRevision.amount_minor == minor,
                            )
                            .order_by(Transaction.updated_at.desc())
                            .limit(5)
                        )
                    )
                    .scalars()
                    .all()
                )
                targets.extend(rows)
            unique = list(dict.fromkeys(targets))
            if len(unique) == 1:
                return unique[0]
            if len(unique) > 1:
                buttons = tuple(
                    (Button(f"Запись {index + 1}", callback("tx", "open", item.hex[:16])),)
                    for index, item in enumerate(unique[:5])
                )
                return [
                    Reply(
                        text=(
                            "Под исправление подходит несколько записей. "
                            "Выберите нужную — наугад ничего не меняю."
                        ),
                        buttons=buttons,
                    )
                ]

        last = (
            await session.execute(
                select(Transaction.id)
                .where(
                    Transaction.workspace_id == workspace_id,
                    Transaction.created_by == actor.user_id,
                    Transaction.status == "posted",
                )
                .order_by(Transaction.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return last


async def _propose_amount_or_date(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    text: str,
) -> list[Reply]:
    """Показать изменение до/после; применение требует подтверждения (FR-33)."""
    workspace_id = actor.require_workspace()
    new_amount: Money | None = None
    match = _AMOUNT_CORRECTION.search(text) or _SIMPLE_CORRECTION.search(text)
    if match:
        amounts = parse_amounts(match.group("new"))
        if amounts:
            new_amount = Money.from_decimal(Decimal(amounts[0].value), workspace.currency)

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        transaction, revision, _spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        current_amount = Money(revision.amount_minor, revision.currency)
        current_date = revision.occurred_date
        entity_version = transaction.entity_version

    local_today = session_local_date(workspace.timezone) if new_amount is None else current_date
    parsed_date = resolve_date_expression(text, reference=local_today)
    new_date = parsed_date.value if parsed_date else None

    if new_amount is None and new_date is None:
        return [
            Reply(
                text=(
                    "Что именно исправить? Укажите новую сумму, например "
                    "«Здесь было 800, а не 1800», или новую дату."
                )
            )
        ]

    lines = ["Изменение записи:"]
    if new_amount is not None:
        lines.append(f"Сумма: {current_amount.format()} → {new_amount.format()}")
    if new_date is not None and new_date != current_date:
        lines.append(f"Дата: {current_date.isoformat()} → {new_date.isoformat()}")
    lines.append("Подтвердите изменение.")
    payload_parts = [transaction_id.hex[:16], str(entity_version)]
    if new_amount is not None:
        payload_parts.append(str(new_amount.minor))
    else:
        payload_parts.append("-")
    payload_parts.append(new_date.isoformat() if new_date else "-")
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("Подтвердить", callback("fix", "apply", *payload_parts)),
                    Button("Отмена", callback("noop", "x")),
                ),
            ),
        )
    ]


def session_local_date(timezone: str):  # type: ignore[no-untyped-def]
    import datetime as dt

    return dt.datetime.now(ZoneInfo(timezone)).date()


async def apply_amount_correction(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    expected_version: int,
    new_amount_minor: int | None,
    new_date_iso: str | None,
) -> list[Reply]:
    """Применить подтверждённое исправление (FR-33, A45, A124)."""
    import datetime as dt

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        _, _, spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        updated = spec
        if new_amount_minor is not None:
            amount = Money(new_amount_minor, spec.amount.currency)
            if len(spec.allocations) != 1:
                raise ConflictError(
                    "У операции несколько распределений: измените суммы частей отдельно"
                )
            allocation = replace(spec.allocations[0], amount=amount)
            legs = tuple(
                replace(
                    leg,
                    signed=Money(
                        -amount.minor if leg.signed.is_negative else amount.minor,
                        amount.currency,
                    ),
                )
                for leg in spec.cash_legs
            )
            updated = replace(updated, amount=amount, allocations=(allocation,), cash_legs=legs)
        if new_date_iso:
            updated = replace(updated, occurred_date=dt.date.fromisoformat(new_date_iso))
        await revise_transaction(
            session,
            uow,
            actor=actor,
            transaction_id=transaction_id,
            new_spec=updated,
            expected_version=expected_version,
        )
    from fintracker.application.conversation.sections import transaction_card_reply

    return await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )


async def _propose_void(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, target: uuid.UUID
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        transaction, revision, _ = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=target
        )
        amount = Money(revision.amount_minor, revision.currency)
        version = transaction.entity_version
    return [
        Reply(
            text=(
                f"Отменить запись {amount.format()} от "
                f"{revision.occurred_date.isoformat()}?\n"
                "Отмена не создаёт банковского возврата, запись останется в истории."
            ),
            buttons=(
                (
                    Button(
                        "Отменить запись",
                        callback("tx", "voidok", target.hex[:16], str(version)),
                    ),
                    Button("Оставить", callback("noop", "x")),
                ),
            ),
        )
    ]


async def apply_void(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    expected_version: int | None,
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        await void_transaction(
            session,
            uow,
            actor=actor,
            transaction_id=transaction_id,
            expected_version=expected_version,
        )
    from fintracker.application.conversation.sections import transaction_card_reply

    replies = await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )
    return [
        Reply(
            text=replies[0].text,
            buttons=(
                (Button("Восстановить", callback("tx", "restore", transaction_id.hex[:16])),),
            ),
        )
    ]


async def apply_restore(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
) -> list[Reply]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        await restore_transaction(session, uow, actor=actor, transaction_id=transaction_id)
    from fintracker.application.conversation.sections import transaction_card_reply

    return await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )


async def apply_note(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    note: str | None,
    mode: str = "replace",
    expected_version: int | None = None,
) -> list[Reply]:
    """Изменить только заметку: деньги и пороговые события не меняются (FR-87).

    Превышение длины не обрезается молча (LIM-04).
    """
    workspace_id = actor.require_workspace()
    if note is not None and len(note) > settings.limits.max_note_chars:
        raise ValidationFailed(
            f"Комментарий длиннее {settings.limits.max_note_chars} символов — "
            "сократите текст, он не обрезается автоматически"
        )
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id)
        _, revision, spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        if mode == "append" and revision.note and note:
            combined: str | None = f"{revision.note}\n{note}"
        elif mode == "delete":
            combined = None
        else:
            combined = note
        if combined is not None:
            stripped = combined.strip()
            combined = stripped or None
        await revise_transaction(
            session,
            uow,
            actor=actor,
            transaction_id=transaction_id,
            new_spec=replace(spec, note=combined),
            expected_version=expected_version,
            change_kind="note_changed",
        )
    from fintracker.application.conversation.sections import transaction_card_reply

    return await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )
