"""Исправление и отмена операций текстом и кнопками (FR-33, FR-34, FR-87)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import replace
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.conversation.keyboards import (
    MAX_CALLBACK_BYTES,
    Button,
    callback,
    short,
)
from fintracker.application.conversation.types import IncomingMessage, Reply
from fintracker.application.delivery.render import format_date
from fintracker.application.delivery.thresholds import refresh_thresholds
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
# «Перенеси в Продукты», «это Рестораны» — исправление категории (FR-27, FR-24).
_CATEGORY_MOVE = re.compile(
    r"(?:перенеси|отнеси|это|поставь|запиши)\s+(?:в|на|как)?\s*(?P<name>[^,.!?]{2,60})",
    re.IGNORECASE,
)


def _with_notice(replies: list[Reply], notice: str) -> list[Reply]:
    if not replies:
        return replies
    first = replies[0]
    return [
        Reply(
            text=f"{notice}\n\n{first.text}",
            buttons=first.buttons,
            transaction_id=first.transaction_id,
        ),
        *replies[1:],
    ]


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
                    "⚠️ Не понял, какую запись исправить.\n\nОтветьте на её карточку "
                    "или откройте историю и выберите операцию."
                ),
                buttons=((Button("🧾 История", callback("menu", "history")),),),
            )
        ]
    if isinstance(target, list):
        return target

    if intent is Intent.CANCEL_TRANSACTION:
        return await _propose_void(settings, actor=actor, workspace=workspace, target=target)
    if intent is Intent.ADD_NOTE:
        match = _NOTE_ADD.search(text)
        if match is None:
            return [Reply(text="✍️ Напишите «Добавь комментарий: текст».")]
        return await apply_note(
            settings,
            actor=actor,
            workspace=workspace,
            transaction_id=target,
            note=match.group("note").strip(),
            mode="append",
        )
    moved = await _propose_category_move(
        settings, actor=actor, workspace=workspace, transaction_id=target, text=text
    )
    if moved is not None:
        return moved
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
            from fintracker.db.models.platform import (
                AuthorReply,
                NotificationDelivery,
                OutboxEvent,
            )

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

            # Карточка, показанная автору сразу после записи (FR-33, G-06).
            replies = (
                (
                    await session.execute(
                        select(AuthorReply).where(
                            AuthorReply.workspace_id == workspace_id,
                            AuthorReply.owner_user_id == actor.user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            wanted = str(message.reply_to_message_id)
            for reply in replies:
                for link in reply.card_links or []:
                    if isinstance(link, dict) and str(link.get("message_id")) == wanted:
                        return uuid.UUID(str(link["transaction_id"]))

            # Неизвестная ссылка не подменяется последней операцией (G-06).
            return [
                Reply(
                    text=(
                        "⚠️ Не нашёл запись, к которой относится этот ответ.\n\nОткройте "
                        "историю и выберите операцию."
                    ),
                    buttons=((Button("🧾 История", callback("menu", "history")),),),
                )
            ]

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
                labels = await _transaction_labels(session, workspace_id, unique[:5])
                buttons = tuple(
                    (Button(labels.get(item, "Запись"), callback("tx", "open", item.hex[:16])),)
                    for item in unique[:5]
                )
                return [
                    Reply(
                        text=(
                            "ℹ️ Подходит несколько записей\n\nВыберите нужную — наугад "
                            "ничего не меняю."
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
                    "✍️ Что именно исправить?\n\nНапишите новую сумму — «исправь 1800 на "
                    "800» — или дату — «это было вчера». Удобнее всего: откройте запись "
                    "и нажмите «✏️ Изменить»."
                ),
                buttons=((Button("🧾 История", callback("menu", "history")),),),
            )
        ]

    lines = ["✏️ Изменить запись?", ""]
    if new_amount is not None:
        lines.append(f"Сумма: {current_amount.format()} → {new_amount.format()}")
    if new_date is not None and new_date != current_date:
        lines.append(
            f"Дата: {format_date(current_date, with_year=True)} → "
            f"{format_date(new_date, with_year=True)}"
        )
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
                    Button("✅ Подтвердить", callback("fix", "apply", *payload_parts)),
                    Button("✕ Отмена", callback("noop", "nochange")),
                ),
            ),
        )
    ]


def session_local_date(timezone: str) -> dt.date:
    return dt.datetime.now(ZoneInfo(timezone)).date()


async def _transaction_labels(
    session: AsyncSession, workspace_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Подписи кнопок выбора: дата, сумма и описание вместо «Запись 1»."""
    rows = (
        await session.execute(
            select(
                Transaction.id,
                TransactionRevision.occurred_date,
                TransactionRevision.amount_minor,
                TransactionRevision.currency,
                TransactionRevision.description,
            )
            .join(
                TransactionRevision,
                (TransactionRevision.workspace_id == Transaction.workspace_id)
                & (TransactionRevision.transaction_id == Transaction.id)
                & (TransactionRevision.revision == Transaction.current_revision),
            )
            .where(Transaction.workspace_id == workspace_id, Transaction.id.in_(ids))
        )
    ).all()
    return {
        row[0]: (
            f"{row[1]:%d.%m} · {Money(row[2], row[3]).format()}"
            + (f" · {row[4][:20]}" if row[4] else "")
        )
        for row in rows
    }


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
        await uow.lock_workspace(workspace_id, actor=actor)
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
        await refresh_thresholds(session, uow, workspace=workspace)
    from fintracker.application.conversation.sections import transaction_card_reply

    replies = await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )
    return _with_notice(replies, "✅ Запись изменена")


async def propose_edit_correction(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    new_amount: Money | None,
    new_date: dt.date | None,
) -> list[Reply] | None:
    """Карточка изменения проведённой операции после правки сообщения (R-03).

    Редакция сообщения адресует исходный ввод: новая независимая трата не
    проводится, изменение требует подтверждения участника (FR-33).
    """
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        transaction, revision, _spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        current_amount = Money(revision.amount_minor, revision.currency)
        current_date = revision.occurred_date
        entity_version = transaction.entity_version

    changed_amount = new_amount if new_amount is not None and new_amount != current_amount else None
    changed_date = new_date if new_date is not None and new_date != current_date else None
    if changed_amount is None and changed_date is None:
        return None

    lines = ["✏️ Сообщение изменено. Обновить запись?", ""]
    if changed_amount is not None:
        lines.append(f"Сумма: {current_amount.format()} → {changed_amount.format()}")
    if changed_date is not None:
        lines.append(
            f"Дата: {format_date(current_date, with_year=True)} → "
            f"{format_date(changed_date, with_year=True)}"
        )
    lines.extend(["", "Вторая трата не создана."])
    payload_parts = [
        transaction_id.hex[:16],
        str(entity_version),
        str(changed_amount.minor) if changed_amount is not None else "-",
        changed_date.isoformat() if changed_date is not None else "-",
    ]
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("✅ Обновить запись", callback("fix", "apply", *payload_parts)),
                    Button("Оставить как есть", callback("noop", "keep")),
                ),
            ),
        )
    ]


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
                f"↩️ Отменить запись {amount.format()} от "
                f"{format_date(revision.occurred_date, with_year=True)}?\n\n"
                "Она перестанет учитываться в расходах, но останется в истории — её "
                "можно будет восстановить. Деньги на карту это не вернёт."
            ),
            buttons=(
                (
                    Button(
                        "↩️ Отменить запись",
                        callback("tx", "voidok", target.hex[:16], str(version)),
                    ),
                    Button("Оставить", callback("noop", "keep")),
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
        await uow.lock_workspace(workspace_id, actor=actor)
        await void_transaction(
            session,
            uow,
            actor=actor,
            transaction_id=transaction_id,
            expected_version=expected_version,
        )
        await refresh_thresholds(session, uow, workspace=workspace)
    from fintracker.application.conversation.sections import transaction_card_reply

    replies = await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )
    return [
        Reply(
            text=replies[0].text,
            buttons=(
                (
                    Button("↩️ Восстановить", callback("tx", "restore", transaction_id.hex[:16])),
                    Button("🧾 История", callback("menu", "history")),
                ),
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
        await uow.lock_workspace(workspace_id, actor=actor)
        await restore_transaction(session, uow, actor=actor, transaction_id=transaction_id)
        await refresh_thresholds(session, uow, workspace=workspace)
    from fintracker.application.conversation.sections import transaction_card_reply

    replies = await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )
    return _with_notice(replies, "✅ Запись восстановлена")


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
        await uow.lock_workspace(workspace_id, actor=actor)
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
        if combined is not None and len(combined) > settings.limits.max_note_chars:
            raise ValidationFailed(
                f"Общий комментарий длиннее {settings.limits.max_note_chars} символов"
            )
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


async def _match_category_name(
    settings: Settings, *, actor: ActorContext, text: str
) -> tuple[uuid.UUID, str] | None:
    """Найти категорию по названию или синониму из текста исправления (FR-27)."""
    from fintracker.application.catalog.categories import list_categories
    from fintracker.application.catalog.normalize import normalize_name

    workspace_id = actor.require_workspace()
    match = _CATEGORY_MOVE.search(text)
    if match is None:
        return None
    wanted = normalize_name(match.group("name"))
    if not wanted:
        return None
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        categories = await list_categories(session, workspace_id=workspace_id)
    best: tuple[uuid.UUID, str] | None = None
    for category in categories:
        normalized = normalize_name(category.name)
        if not normalized:
            continue
        matches = normalized == wanted or normalized in wanted
        if matches and (best is None or len(normalized) > len(normalize_name(best[1]))):
            best = (category.id, category.name)
    return best


async def _propose_category_move(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    text: str,
) -> list[Reply] | None:
    """Показать перенос в другую категорию до применения (FR-27, FR-33)."""
    target = await _match_category_name(settings, actor=actor, text=text)
    if target is None:
        return await _offer_new_category(
            settings, actor=actor, workspace=workspace, transaction_id=transaction_id, text=text
        )
    category_id, category_name = target

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        transaction, _revision, spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        if len(spec.allocations) != 1:
            return [
                Reply(
                    text=(
                        "ℹ️ У операции несколько частей: откройте карточку и измените "
                        "нужную часть отдельно."
                    ),
                    buttons=(
                        (
                            Button(
                                "🧾 Открыть карточку",
                                callback("tx", "open", transaction_id.hex[:16]),
                            ),
                        ),
                    ),
                )
            ]
        current_category_id = spec.allocations[0].category_id
        version = transaction.entity_version
        current_name = "без категории"
        if current_category_id is not None:
            from fintracker.db.models.catalog import Category

            row = (
                await session.execute(
                    select(Category.name).where(
                        Category.workspace_id == workspace_id,
                        Category.id == current_category_id,
                    )
                )
            ).scalar_one_or_none()
            current_name = row or current_name

    if current_category_id == category_id:
        return [Reply(text=f"ℹ️ Операция уже отнесена к категории «{category_name}».")]
    return [
        Reply(
            text=(
                f"🗂 Перенести запись в «{category_name}»?\n\n"
                f"Сейчас: {current_name}. Другие записи не изменятся."
            ),
            buttons=(
                (
                    Button(
                        "✅ Подтвердить",
                        callback(
                            "fix", "cat", transaction_id.hex[:16], str(version), short(category_id)
                        ),
                    ),
                    Button("✕ Отмена", callback("noop", "nochange")),
                ),
            ),
        )
    ]


async def apply_category_correction(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    expected_version: int,
    category_id: uuid.UUID,
) -> list[Reply]:
    """Применить перенос и предложить запомнить правило (FR-24, FR-27)."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        _, revision, spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        allocation = replace(spec.allocations[0], category_id=category_id)
        await revise_transaction(
            session,
            uow,
            actor=actor,
            transaction_id=transaction_id,
            new_spec=replace(spec, allocations=(allocation,)),
            expected_version=expected_version,
        )
        await refresh_thresholds(session, uow, workspace=workspace)
        keyword = (revision.merchant or revision.description or revision.note or "").strip()

    from fintracker.application.conversation.sections import transaction_card_reply

    replies = await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=transaction_id
    )
    replies = _with_notice(replies, "✅ Категория изменена")
    if not keyword:
        return replies
    # Однократная покупка не переназначает прошлые расходы (FR-24).
    offer = Reply(
        text=(
            f"🧠 Запомнить: «{keyword[:40]}» — всегда в эту категорию?\n\n"
            "Правило сработает для новых записей, старые не изменятся."
        ),
        buttons=(
            (
                Button(
                    "Всегда сюда",
                    callback("fix", "rule", transaction_id.hex[:16], short(category_id)),
                ),
                Button("Только эту", callback("noop", "once")),
            ),
        ),
    )
    return [*replies, offer]


async def remember_category_rule(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    category_id: uuid.UUID,
) -> list[Reply]:
    """Сохранить личное правило по ключевому слову записи (FR-24, CMD-27)."""
    from fintracker.application.catalog.rules import learn_from_correction

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        _, revision, _ = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        keyword = (revision.merchant or revision.description or revision.note or "").strip()
        if not keyword:
            return [Reply(text="⚠️ Не удалось определить условие правила.")]
        rule = await learn_from_correction(
            session,
            uow,
            actor=actor,
            keyword=keyword,
            category_id=category_id,
        )
    return [
        Reply(
            text=(
                f"✅ Запомнил: «{rule.keyword}» → {rule.category_name}\n\n"
                "Правило личное и действует для новых записей. Изменить его можно "
                "в «Мои настройки» → «Правила»."
            ),
            buttons=((Button("🧠 Мои правила", callback("set", "rules")),),),
        )
    ]


async def _offer_new_category(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    text: str,
) -> list[Reply] | None:
    """«Создать категорию и перенести сюда эту запись» (A115, FR-21, FR-33).

    Общий расход при этом не меняется: переносится только принадлежность
    записи к категории.
    """
    from fintracker.application.catalog.normalize import normalize_name

    match = _CATEGORY_MOVE.search(text)
    if match is None:
        return None
    name = " ".join(match.group("name").split())[:40]
    if not normalize_name(name):
        return None

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        transaction, _revision, spec = await load_current_spec(
            session, workspace_id=workspace_id, transaction_id=transaction_id
        )
        if len(spec.allocations) != 1:
            return None
        version = transaction.entity_version

    data = callback("fix", "newcat", transaction_id.hex[:16], str(version), name)
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        return [
            Reply(
                text=(
                    "ℹ️ Категории «"
                    f"{name}"
                    "» пока нет.\n\nСоздайте её сообщением «Создай категорию "
                    f"{name}"
                    "», затем повторите перенос."
                )
            )
        ]
    return [
        Reply(
            text=(
                "✍️ Категории «"
                f"{name}"
                "» пока нет.\n\nСоздать её и перенести сюда эту запись?\n\nОбщий "
                "расход не изменится."
            ),
            buttons=(
                (
                    Button("Создать и перенести", data),
                    Button("✕ Отмена", callback("noop", "nochange")),
                ),
            ),
        )
    ]


async def create_category_and_move(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    transaction_id: uuid.UUID,
    expected_version: int,
    name: str,
) -> list[Reply]:
    """Создать категорию и перенести в неё запись одним подтверждением (A115)."""
    from fintracker.application.catalog.categories import create_category

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        view = await create_category(session, uow, actor=actor, name=name)
        category_id = view.id
    return await apply_category_correction(
        settings,
        actor=actor,
        workspace=workspace,
        transaction_id=transaction_id,
        expected_version=expected_version,
        category_id=category_id,
    )
