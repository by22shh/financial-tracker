"""Плановые платежи в диалоге (FR-45, FR-46).

Наступление даты создаёт ожидаемый платёж, а не расход. Оплата записывается
одним нажатием «Оплачено»: появляется обычный расход, связанный с этим
экземпляром. Другую сумму можно отправить сообщением.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, select

from fintracker.application.commitments.schedules import (
    archive_schedule,
    change_occurrence,
    materialize_occurrences,
    upcoming_payments,
)
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.conversation.views import format_date, money
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import DomainError, NotFound
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.commitments import Occurrence, ScheduledItem, ScheduleVersion
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

POSTPONE_DAYS = 3
PAYMENTS_PAGE_SIZE = 6
HORIZON_DAYS = 45


def _menu_row() -> tuple[Button, ...]:
    return (
        Button("➕ Новый платёж", callback("pay", "new")),
        Button("⚙️ Все платежи", callback("pay", "list")),
    )


async def payments_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, page: int = 0
) -> list[Reply]:
    """Ближайшие и просроченные платежи (FR-45)."""
    from fintracker.application.conversation import views

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        # Новый платёж виден сразу, не дожидаясь фоновой задачи напоминаний.
        await materialize_occurrences(
            session, workspace_id=workspace_id, until_date=today + dt.timedelta(days=HORIZON_DAYS)
        )
        payments = await upcoming_payments(
            session,
            workspace_id=workspace_id,
            today=today,
            horizon_days=HORIZON_DAYS,
            currency=workspace.currency,
        )
        has_schedules = (
            await session.execute(
                select(ScheduledItem.id)
                .where(
                    ScheduledItem.workspace_id == workspace_id,
                    ScheduledItem.direction == "payment",
                    ScheduledItem.archived_at.is_(None),
                )
                .limit(1)
            )
        ).first() is not None
    back = (Button("← Меню", callback("menu", "main")),)
    if not payments:
        text = (
            views.empty_state("payments")
            if not has_schedules
            else (
                "🗓 Платежи\n\nВ ближайшие полтора месяца платить ничего не нужно. "
                "Все настроенные платежи — в «⚙️ Все платежи»."
            )
        )
        rows = (
            (_menu_row(), back)
            if has_schedules
            else (
                (Button("➕ Новый платёж", callback("pay", "new")),),
                back,
            )
        )
        return [Reply(text=text, buttons=rows)]
    lines = ["🗓 Ближайшие платежи", ""]
    buttons: list[tuple[Button, ...]] = []
    last_page = (len(payments) - 1) // PAYMENTS_PAGE_SIZE
    page = max(0, min(page, last_page))
    start = page * PAYMENTS_PAGE_SIZE
    for item in payments[start : start + PAYMENTS_PAGE_SIZE]:
        due = format_date(item.due_date, with_year=item.due_date.year != today.year)
        when = f"просрочен с {due}" if item.is_overdue else due
        amount = (
            money(item.remaining_minor, item.currency)
            if item.expected_minor is not None
            else "сумма не задана"
        )
        lines.append(f"• {item.schedule_name} — {when}, {amount}")
        buttons.append(
            (
                Button(
                    f"{'⚠️ ' if item.is_overdue else ''}{item.schedule_name[:22]} · "
                    f"{item.due_date:%d.%m}",
                    callback("pay", "open", short(item.occurrence_id)),
                ),
            )
        )
    lines.extend(["", "Нажмите на платёж, чтобы отметить оплату, перенести или пропустить."])
    lines.append("До оплаты платёж не входит в расходы.")
    if last_page:
        lines.append(f"\nСтраница {page + 1} из {last_page + 1}")
        navigation = []
        if page:
            navigation.append(Button("← Назад", callback("pay", "page", str(page - 1))))
        if page < last_page:
            navigation.append(Button("Далее →", callback("pay", "page", str(page + 1))))
        buttons.append(tuple(navigation))
    buttons.append(_menu_row())
    buttons.append(back)
    return [Reply(text="\n".join(lines), buttons=tuple(buttons))]


async def _find_occurrence(
    settings: Settings, *, actor: ActorContext, prefix: str
) -> tuple[Occurrence, str, uuid.UUID | None]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            await session.execute(
                select(Occurrence, ScheduledItem.name, ScheduleVersion.category_id)
                .join(
                    ScheduledItem,
                    (ScheduledItem.workspace_id == Occurrence.workspace_id)
                    & (ScheduledItem.id == Occurrence.schedule_id),
                )
                .join(
                    ScheduleVersion,
                    (ScheduleVersion.workspace_id == Occurrence.workspace_id)
                    & (ScheduleVersion.schedule_id == Occurrence.schedule_id)
                    & (ScheduleVersion.version == Occurrence.schedule_version),
                )
                .where(
                    Occurrence.workspace_id == workspace_id,
                    func.replace(cast(Occurrence.id, String), "-", "").like(
                        f"{prefix[:16]}%" if re.fullmatch(r"[0-9a-f]{8,32}", prefix) else "-"
                    ),
                )
                .limit(2)
            )
        ).all()
        row = rows[0] if len(rows) == 1 else None
        if row is None:
            raise NotFound("Платёж не найден. Откройте раздел «Платежи» заново.")
        occurrence, name, category_id = row
        session.expunge(occurrence)
    return occurrence, name, category_id


async def occurrence_card(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, prefix: str
) -> list[Reply]:
    occurrence, name, _ = await _find_occurrence(settings, actor=actor, prefix=prefix)
    code = short(occurrence.id)
    if occurrence.state not in {"planned", "partially_settled"}:
        state = {
            "settled": "уже оплачен",
            "skipped": "пропущен",
            "cancelled": "отменён",
        }.get(occurrence.state, "закрыт")
        return [
            Reply(
                text=f"ℹ️ Платёж «{name}» на {format_date(occurrence.due_date)} {state}.",
                buttons=((Button("🗓 Платежи", callback("menu", "payments")),),),
            )
        ]
    remaining = (occurrence.expected_minor or 0) - occurrence.settled_minor
    lines = [f"🗓 {name}", "", f"Срок: {format_date(occurrence.due_date, with_year=True)}"]
    if occurrence.expected_minor is not None:
        lines.append(f"Сумма: {money(occurrence.expected_minor, workspace.currency)}")
        if occurrence.settled_minor:
            lines.append(f"Уже оплачено: {money(occurrence.settled_minor, workspace.currency)}")
    lines.extend(["", "Оплатили? Одно нажатие запишет расход и закроет платёж."])
    rows: list[tuple[Button, ...]] = []
    if occurrence.expected_minor is not None and remaining > 0:
        rows.append(
            (
                Button(
                    f"✅ Оплачено {money(remaining, workspace.currency)}",
                    callback("pay", "settle", code),
                ),
            )
        )
    rows.append((Button("✍️ Другая сумма", callback("pay", "done", code)),))
    rows.append(
        (
            Button(f"📅 Перенести на {POSTPONE_DAYS} дня", callback("pay", "move", code)),
            Button("⏭ Пропустить", callback("pay", "skip", code)),
        )
    )
    rows.append((Button("← Платежи", callback("menu", "payments")),))
    return [Reply(text="\n".join(lines), buttons=tuple(rows))]


async def settle_now(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, prefix: str
) -> list[Reply]:
    """Оплата одним нажатием: расход на остаток и закрытие экземпляра (FR-46)."""
    from fintracker.application.commitments.schedules import settle_occurrence
    from fintracker.application.conversation.sections import transaction_card_reply
    from fintracker.application.delivery.thresholds import refresh_thresholds
    from fintracker.application.ledger.service import post_transaction
    from fintracker.db.models.ledger import FinancialEffect
    from fintracker.domain.ledger.model import (
        AllocationRole,
        AllocationSpec,
        CashLegSpec,
        CoverageMode,
        TransactionSpec,
        TransactionType,
    )

    occurrence, name, category_id = await _find_occurrence(settings, actor=actor, prefix=prefix)
    remaining = (occurrence.expected_minor or 0) - occurrence.settled_minor
    if occurrence.state not in {"planned", "partially_settled"} or remaining <= 0:
        return await occurrence_card(settings, actor=actor, workspace=workspace, prefix=prefix)
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    amount = Money(remaining, workspace.currency)
    spec = TransactionSpec(
        transaction_type=TransactionType.EXPENSE,
        amount=amount,
        occurred_date=today,
        timezone=workspace.timezone,
        description=name,
        allocations=(
            AllocationSpec(role=AllocationRole.EXPENSE, amount=amount, category_id=category_id),
        ),
        cash_legs=(CashLegSpec(signed=-amount, coverage=CoverageMode.UNKNOWN),),
    )
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        result = await post_transaction(
            session, uow, actor=actor, spec=spec, origin="telegram_text"
        )
        effect = (
            await session.execute(
                select(FinancialEffect).where(
                    FinancialEffect.workspace_id == workspace_id,
                    FinancialEffect.transaction_id == result.transaction_id,
                    FinancialEffect.is_active.is_(True),
                )
            )
        ).scalar_one()
        await settle_occurrence(
            session,
            uow,
            actor=actor,
            occurrence_id=occurrence.id,
            effect_id=effect.id,
            transaction_id=result.transaction_id,
            amount=amount,
        )
        await refresh_thresholds(session, uow, workspace=workspace)
    replies = await transaction_card_reply(
        settings,
        actor=actor,
        workspace=workspace,
        transaction_id=result.transaction_id,
        confirmation=True,
    )
    first = replies[0]
    return [
        Reply(
            text=f"✅ Платёж «{name}» оплачен\n\n{first.text}",
            buttons=first.buttons,
            transaction_id=first.transaction_id,
        ),
        *replies[1:],
    ]


async def schedules_list(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Все настроенные платежи: открыть, чтобы посмотреть или удалить."""
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            await session.execute(
                select(ScheduledItem, ScheduleVersion)
                .join(
                    ScheduleVersion,
                    (ScheduleVersion.workspace_id == ScheduledItem.workspace_id)
                    & (ScheduleVersion.schedule_id == ScheduledItem.id)
                    & (ScheduleVersion.version == ScheduledItem.current_version),
                )
                .where(
                    ScheduledItem.workspace_id == workspace_id,
                    ScheduledItem.direction == "payment",
                    ScheduledItem.archived_at.is_(None),
                )
                .order_by(ScheduledItem.name)
            )
        ).all()
    if not rows:
        return await payments_view(settings, actor=actor, workspace=workspace)
    lines = ["⚙️ Все платежи", ""]
    buttons: list[tuple[Button, ...]] = []
    for item, version in rows[:20]:
        amount = (
            money(version.expected_minor, version.currency)
            if version.expected_minor is not None
            else "сумма не задана"
        )
        lines.append(f"• {item.name} — {amount}, {_repeat_label(version)}")
        buttons.append((Button(item.name[:30], callback("pay", "sched", short(item.id))),))
    buttons.append((Button("➕ Новый платёж", callback("pay", "new")),))
    buttons.append((Button("← Платежи", callback("menu", "payments")),))
    return [Reply(text="\n".join(lines), buttons=tuple(buttons))]


def _repeat_label(version: ScheduleVersion) -> str:
    if version.rule_kind == "once":
        return f"один раз, {format_date(version.anchor_date)}"
    if version.rule_kind == "monthly":
        return f"каждый месяц, {version.anchor_date.day} числа"
    if version.rule_kind == "weekly":
        return "каждую неделю"
    if version.rule_kind == "yearly":
        return f"каждый год, {format_date(version.anchor_date)}"
    return "по расписанию"


async def _find_schedule(
    settings: Settings, *, actor: ActorContext, prefix: str
) -> tuple[uuid.UUID, str, ScheduleVersion]:
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        rows = (
            await session.execute(
                select(ScheduledItem, ScheduleVersion)
                .join(
                    ScheduleVersion,
                    (ScheduleVersion.workspace_id == ScheduledItem.workspace_id)
                    & (ScheduleVersion.schedule_id == ScheduledItem.id)
                    & (ScheduleVersion.version == ScheduledItem.current_version),
                )
                .where(
                    ScheduledItem.workspace_id == workspace_id,
                    ScheduledItem.archived_at.is_(None),
                )
            )
        ).all()
        match = next(((item, version) for item, version in rows if short(item.id) == prefix), None)
        if match is None:
            raise NotFound("Платёж не найден или уже удалён")
        session.expunge(match[1])
    return match[0].id, match[0].name, match[1]


async def schedule_card(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, prefix: str
) -> list[Reply]:
    from fintracker.application.conversation.context import category_paths

    _, name, version = await _find_schedule(settings, actor=actor, prefix=prefix)
    category = "Без категории"
    if version.category_id is not None:
        async with session_scope(
            settings,
            RuntimeRole.API,
            user_id=actor.user_id,
            workspace_id=actor.require_workspace(),
        ) as session:
            category = (await category_paths(session, workspace_id=actor.require_workspace())).get(
                version.category_id, category
            )
    amount = (
        money(version.expected_minor, version.currency)
        if version.expected_minor is not None
        else "не задана"
    )
    return [
        Reply(
            text=(
                f"🗓 {name}\n\nСумма: {amount}\nПовтор: {_repeat_label(version)}\n"
                f"Категория расхода: {category}\n\nОплата записывается расходом в этой "
                "категории. Удаление отменит будущие напоминания, прошлые оплаты останутся."
            ),
            buttons=(
                (Button("🗑 Удалить платёж", callback("pay", "del", prefix)),),
                (Button("← Все платежи", callback("pay", "list")),),
            ),
        )
    ]


async def payment_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Кнопки раздела и напоминания: оплата, перенос, пропуск, управление."""
    from fintracker.application.conversation.pending import set_pending

    workspace_id = actor.require_workspace()
    if action == "page":
        page = int(rest[0]) if rest and rest[0].isdigit() else 0
        return await payments_view(settings, actor=actor, workspace=workspace, page=page)
    if action == "new":
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind="payment_new",
            payload={"step": "name"},
        )
        return [
            Reply(
                text=(
                    "🗓 Новый платёж\n\nКак он называется?\n\nНапример: Интернет или Аренда. "
                    "Следующим сообщением я спрошу сумму."
                ),
                buttons=((Button("✕ Отмена", callback("noop", "nopay")),),),
            )
        ]
    if action == "list":
        return await schedules_list(settings, actor=actor, workspace=workspace)
    if action == "rep" and rest:
        return await _finish_new_payment(settings, actor=actor, workspace=workspace, repeat=rest[0])
    if not rest:
        return [Reply(text="🔄 Кнопка устарела.\n\nОткройте раздел «Платежи».")]
    prefix = rest[0]
    if action == "sched":
        return await schedule_card(settings, actor=actor, workspace=workspace, prefix=prefix)
    if action == "del":
        _, name, _ = await _find_schedule(settings, actor=actor, prefix=prefix)
        return [
            Reply(
                text=(
                    f"🗑 Удалить платёж «{name}»?\n\nНапоминаний больше не будет. "
                    "Записанные оплаты останутся в истории."
                ),
                buttons=(
                    (
                        Button("🗑 Удалить", callback("pay", "delok", prefix)),
                        Button("Отмена", callback("pay", "sched", prefix)),
                    ),
                ),
            )
        ]
    if action == "delok":
        schedule_id, name, _ = await _find_schedule(settings, actor=actor, prefix=prefix)
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
            await uow.lock_workspace(workspace_id, actor=actor)
            await archive_schedule(session, uow, actor=actor, schedule_id=schedule_id)
        return [
            Reply(
                text=f"🗑 Платёж «{name}» удалён. Напоминаний по нему больше не будет.",
                buttons=((Button("🗓 Платежи", callback("menu", "payments")),),),
            )
        ]
    if action == "open":
        return await occurrence_card(settings, actor=actor, workspace=workspace, prefix=prefix)
    if action == "settle":
        return await settle_now(settings, actor=actor, workspace=workspace, prefix=prefix)

    occurrence, name, _ = await _find_occurrence(settings, actor=actor, prefix=prefix)
    if action in {"move", "skip"}:
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
            await uow.lock_workspace(workspace_id, actor=actor)
            try:
                if action == "move":
                    await change_occurrence(
                        session,
                        uow,
                        actor=actor,
                        occurrence_id=occurrence.id,
                        action="postpone",
                        new_due_date=occurrence.due_date + dt.timedelta(days=POSTPONE_DAYS),
                        reason="Перенос из напоминания",
                    )
                else:
                    await change_occurrence(
                        session,
                        uow,
                        actor=actor,
                        occurrence_id=occurrence.id,
                        action="skip",
                        reason="Пропуск из напоминания",
                    )
            except DomainError as exc:
                return [
                    Reply(
                        text=f"⚠️ {exc.message}",
                        buttons=((Button("🗓 Платежи", callback("menu", "payments")),),),
                    )
                ]
        if action == "move":
            moved = occurrence.due_date + dt.timedelta(days=POSTPONE_DAYS)
            return [
                Reply(
                    text=(
                        f"📅 Платёж «{name}» перенесён на {format_date(moved)}\n\n"
                        "Расход не записан: изменился только срок."
                    ),
                    buttons=((Button("🗓 Платежи", callback("menu", "payments")),),),
                )
            ]
        return [
            Reply(
                text=(
                    f"⏭ Платёж «{name}» пропущен в этот раз\n\nСледующий придёт по "
                    "расписанию. Расход не записан."
                ),
                buttons=((Button("🗓 Платежи", callback("menu", "payments")),),),
            )
        ]

    remaining = (occurrence.expected_minor or 0) - occurrence.settled_minor
    amount_hint = (
        money(remaining, workspace.currency) if occurrence.expected_minor is not None else "500"
    )
    if action in {"paid", "done"}:
        # Следующая подтверждённая трата закроет именно этот экземпляр (FR-46).
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind="occurrence_settle",
            payload={"occurrence_id": str(occurrence.id)},
        )
    return [
        Reply(
            text=(
                f"✍️ Сколько заплатили за «{name}»?\n\nОтправьте сумму с описанием, "
                f"например «{name.lower()} {amount_hint}». Платёж закроется после записи, "
                "лишней траты не появится."
            ),
            buttons=((Button("← Платежи", callback("menu", "payments")),),),
        )
    ]


_REMINDER_WORDS = re.compile(
    r"\b(напомни(?:ть)?|мне|пожалуйста|оплатить|заплатить|оплата|платёж|платеж|про|об?|за|"
    r"каждый|месяц|числа|число)\b",
    re.IGNORECASE,
)


async def start_payment_from_reminder(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, text: str
) -> list[Reply]:
    """«Напомни оплатить интернет 900 25 числа» — платёж с уже известными полями."""
    from fintracker.application.conversation.pending import set_pending
    from fintracker.domain.parsing.amounts import parse_amounts
    from fintracker.domain.parsing.dates import resolve_date_expression

    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    amounts = parse_amounts(text)
    rest = text
    amount_value: str | None = None
    if amounts:
        amount_value = str(amounts[0].value)
        rest = rest.replace(amounts[0].raw, " ", 1)
    due: dt.date | None = None
    day_only = re.search(r"\b(\d{1,2})\s*(?:-?го\s*)?числа\b", rest)
    if day_only:
        day = int(day_only.group(1))
        if 1 <= day <= 28:
            due = today.replace(day=day)
            if due < today:
                due = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=day)
        rest = rest.replace(day_only.group(0), " ")
    else:
        parsed = resolve_date_expression(rest, reference=today, prefer_future=True)
        if parsed is not None:
            due = parsed.value
            rest = re.sub(re.escape(parsed.expression), " ", rest, count=1, flags=re.IGNORECASE)
    name = " ".join(_REMINDER_WORDS.sub(" ", rest).split()).strip(" ,.:-")
    name = name[:1].upper() + name[1:] if name else ""
    payload: dict[str, Any] = {"step": "name"}
    if name:
        payload = {"step": "amount", "name": name[:120]}
        if amount_value:
            payload = {**payload, "step": "date", "amount": amount_value}
            if due is not None:
                payload = {**payload, "step": "repeat", "due": due.isoformat()}
    await set_pending(
        settings,
        user_id=actor.user_id,
        workspace_id=actor.require_workspace(),
        kind="payment_new",
        payload=payload,
    )
    return _next_payment_prompt(payload, workspace)


def _next_payment_prompt(payload: dict[str, Any], workspace: Workspace) -> list[Reply]:
    cancel = (Button("✕ Отмена", callback("noop", "nopay")),)
    step = payload.get("step")
    name = str(payload.get("name") or "")
    known = [f"Название: {name}"] if name else []
    if payload.get("amount"):
        amount = Money.from_decimal(Decimal(str(payload["amount"])), workspace.currency)
        known.append(f"Сумма: {amount.format()}")
    if payload.get("due"):
        due = dt.date.fromisoformat(str(payload["due"]))
        known.append(f"Срок: {format_date(due, with_year=True)}")
    header = "🗓 Новый платёж" + ("\n\n" + "\n".join(known) if known else "")
    if step == "name":
        return [
            Reply(
                text=f"{header}\n\nКак он называется? Например: Интернет или Аренда.",
                buttons=(cancel,),
                retry_input=True,
            )
        ]
    if step == "amount":
        return [
            Reply(
                text=f"{header}\n\nКакая сумма? Отправьте число, например 900.",
                buttons=(cancel,),
                retry_input=True,
            )
        ]
    if step == "date":
        return [
            Reply(
                text=(
                    f"{header}\n\n📅 Когда платить? Отправьте дату: «25 сентября», «25.09» "
                    "или «сегодня»."
                ),
                buttons=(cancel,),
                retry_input=True,
            )
        ]
    return [
        Reply(
            text=f"{header}\n\n🔁 Как часто платить?",
            buttons=(
                (
                    Button("Каждый месяц", callback("pay", "rep", "m")),
                    Button("Один раз", callback("pay", "rep", "o")),
                ),
                cancel,
            ),
            # Ожидание сохраняется до выбора повторения кнопкой.
            retry_input=True,
        )
    ]


async def create_payment_from_text(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    text: str,
    pending_payload: dict[str, Any] | None = None,
) -> list[Reply]:
    """Создать платёж коротким диалогом; пакетный формат тоже поддерживается."""
    from fintracker.application.conversation.guards import single_amount
    from fintracker.application.conversation.pending import set_pending
    from fintracker.domain.parsing.amounts import parse_amounts
    from fintracker.domain.parsing.dates import resolve_date_expression

    payload = dict(pending_payload or {})
    step = payload.get("step") or "name"
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()

    if "=" in text and step == "name":
        # Пакетный формат «Интернет = 900 = 20.09» остаётся доступным.
        parts = [item.strip() for item in text.split("=")]
        amounts = parse_amounts(parts[1]) if len(parts) > 1 else []
        if not parts[0] or not amounts:
            return [
                Reply(
                    text="✍️ Нужны название и сумма: «Интернет = 900 = 20.09».",
                    retry_input=True,
                )
            ]
        payload = {"step": "date", "name": parts[0][:120], "amount": str(amounts[0].value)}
        if len(parts) > 2 and parts[2]:
            parsed = resolve_date_expression(parts[2], reference=today, prefer_future=True)
            if parsed is None:
                return [Reply(text="📅 Не понял дату. Например: 20.09.", retry_input=True)]
            payload = {**payload, "step": "repeat", "due": parsed.value.isoformat()}
    elif step == "name":
        name = text.strip()
        if not name or (parse_amounts(name) and single_amount(name)):
            return [
                Reply(
                    text="🗓 Как называется платёж?\n\nНапример: Интернет или Аренда.",
                    retry_input=True,
                )
            ]
        payload = {"step": "amount", "name": name[:120]}
    elif step == "amount":
        parsed_amounts = parse_amounts(text)
        amount = single_amount(text) or (parsed_amounts[0] if parsed_amounts else None)
        if amount is None or amount.value <= 0:
            return [
                Reply(
                    text=f"✍️ Укажите сумму в {workspace.currency}, например 900.",
                    retry_input=True,
                )
            ]
        payload = {**payload, "step": "date", "amount": str(amount.value)}
    elif step == "date":
        parsed = resolve_date_expression(text, reference=today, prefer_future=True)
        if parsed is None:
            return [
                Reply(
                    text="📅 Не понял дату. Отправьте, например, «25 сентября» или «25.09».",
                    retry_input=True,
                )
            ]
        payload = {**payload, "step": "repeat", "due": parsed.value.isoformat()}
    else:
        return [
            Reply(
                text="🔁 Выберите кнопкой, как часто платить.",
                buttons=_next_payment_prompt(payload, workspace)[0].buttons,
                retry_input=True,
            )
        ]
    await set_pending(
        settings,
        user_id=actor.user_id,
        workspace_id=workspace_id,
        kind="payment_new",
        payload=payload,
    )
    return _next_payment_prompt(payload, workspace)


async def _finish_new_payment(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, repeat: str
) -> list[Reply]:
    from fintracker.application.catalog.categories import list_categories
    from fintracker.application.catalog.keywords import suggest_category
    from fintracker.application.commitments.schedules import create_schedule
    from fintracker.application.conversation.pending import clear_pending, peek_pending
    from fintracker.domain.schedule import ScheduleKind, ScheduleRule

    workspace_id = actor.require_workspace()
    pending = await peek_pending(settings, user_id=actor.user_id, workspace_id=workspace_id)
    if pending is None or pending.kind != "payment_new" or pending.payload.get("step") != "repeat":
        return [
            Reply(
                text="🔄 Этот платёж уже создан или отменён.",
                buttons=((Button("🗓 Платежи", callback("menu", "payments")),),),
            )
        ]
    payload = pending.payload
    name = str(payload.get("name") or "Платёж")
    expected = Money.from_decimal(Decimal(str(payload["amount"])), workspace.currency)
    due = dt.date.fromisoformat(str(payload["due"]))
    kind = ScheduleKind.ONCE if repeat == "o" else ScheduleKind.MONTHLY
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        categories = await list_categories(session, workspace_id=workspace_id)
        category_id = suggest_category(name, ((item.id, item.name) for item in categories))
        category_name = next(
            (item.name for item in categories if item.id == category_id), "Без категории"
        )
        try:
            item = await create_schedule(
                session,
                uow,
                actor=actor,
                name=name[:120],
                direction="payment",
                rule=ScheduleRule(kind=kind, anchor_date=due),
                currency=workspace.currency,
                expected=expected,
                category_id=category_id,
            )
        except DomainError as exc:
            return [Reply(text=f"⚠️ {exc.message}")]
        item_name = item.name
    await clear_pending(settings, user_id=actor.user_id, workspace_id=workspace_id)
    repeat_text = f"каждый месяц, {due.day} числа" if kind is ScheduleKind.MONTHLY else "один раз"
    return [
        Reply(
            text=(
                f"✅ Платёж «{item_name}» создан\n\n"
                f"Сумма: {expected.format()}\n"
                f"Ближайший срок: {format_date(due, with_year=True)}\n"
                f"Повтор: {repeat_text}\n"
                f"Категория расхода: {category_name}\n\n"
                "Накануне бот напомнит о сроке. Расход появится, когда вы отметите оплату."
            ),
            buttons=((Button("🗓 Платежи", callback("menu", "payments")),),),
        )
    ]
