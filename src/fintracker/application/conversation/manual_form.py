"""Ручная форма ввода, работающая без AI (FR-10, NFR-14, A103)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.ledger.service import post_transaction
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.errors import ValidationFailed
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.domain.ledger.model import (
    AllocationRole,
    AllocationSpec,
    CashLegSpec,
    CoverageMode,
    TransactionSpec,
    TransactionType,
)
from fintracker.domain.parsing.amounts import parse_amounts
from fintracker.domain.parsing.dates import resolve_date_expression

FORM_HELP = """➕ Новая трата

Отправьте сумму, например 450. Категорию, дату и комментарий выберете кнопками.

Можно и одним сообщением: «такси 450»."""

CATEGORY_PAGE = 8


def _cancel_row() -> tuple[Button, ...]:
    return (Button("✕ Отменить", callback("mf", "cancel")),)


async def start_manual_form(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.conversation.pending import set_pending

    await set_pending(
        settings,
        user_id=actor.user_id,
        workspace_id=actor.require_workspace(),
        kind="manual_form",
        payload={"step": "amount"},
    )
    return [Reply(text=FORM_HELP, buttons=(_cancel_row(),))]


async def _category_prompt(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    payload: dict[str, Any],
    page: int = 0,
    notice: str | None = None,
) -> list[Reply]:
    from fintracker.application.catalog.categories import list_categories

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        categories = await list_categories(session, workspace_id=workspace_id)
    last_page = max(0, (len(categories) - 1) // CATEGORY_PAGE)
    page = max(0, min(page, last_page))
    chunk = categories[page * CATEGORY_PAGE : (page + 1) * CATEGORY_PAGE]
    rows: list[tuple[Button, ...]] = [
        tuple(
            Button(item.name[:28], callback("mf", "cat", short(item.id)))
            for item in chunk[index : index + 2]
        )
        for index in range(0, len(chunk), 2)
    ]
    paging: list[Button] = []
    if page:
        paging.append(Button("← Назад", callback("mf", "cpage", str(page - 1))))
    if page < last_page:
        paging.append(Button("Далее →", callback("mf", "cpage", str(page + 1))))
    if paging:
        rows.append(tuple(paging))
    rows.append((Button("Без категории", callback("mf", "cat", "-")),))
    rows.append(_cancel_row())
    amount = Money.from_decimal(Decimal(str(payload["amount"])), workspace.currency).format()
    text = f"🗂 Категория для {amount}\n\nВыберите кнопкой или отправьте название."
    if notice:
        text = f"{notice}\n\n{text}"
    return [Reply(text=text, buttons=tuple(rows), retry_input=True)]


def _date_prompt(category_name: str | None) -> list[Reply]:
    chosen = f"Категория: {category_name}\n\n" if category_name else ""
    return [
        Reply(
            text=(
                f"{chosen}📅 Когда была трата?\n\n"
                "Выберите кнопкой или отправьте дату, например 20.09."
            ),
            buttons=(
                (
                    Button("Сегодня", callback("mf", "date", "0")),
                    Button("Вчера", callback("mf", "date", "1")),
                    Button("Позавчера", callback("mf", "date", "2")),
                ),
                _cancel_row(),
            ),
            retry_input=True,
        )
    ]


def _comment_prompt() -> list[Reply]:
    return [
        Reply(
            text=(
                "💬 Комментарий\n\nОтправьте текст — его увидят участники бюджета — "
                "или сохраните трату без комментария."
            ),
            buttons=(
                (Button("✅ Сохранить без комментария", callback("mf", "note", "-")),),
                _cancel_row(),
            ),
            retry_input=True,
        )
    ]


async def continue_manual_form(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    text: str,
    payload: dict[str, Any],
) -> list[Reply]:
    """Collect one field at a time so mobile users need no separators."""
    from fintracker.application.catalog.categories import list_categories
    from fintracker.application.catalog.normalize import normalize_name
    from fintracker.application.conversation.pending import set_pending

    workspace_id = actor.require_workspace()
    step = str(payload.get("step") or "amount")
    if step == "amount":
        if "|" in text:
            return await submit_manual_form(settings, actor=actor, workspace=workspace, text=text)
        amounts = parse_amounts(text)
        if not amounts or amounts[0].value <= 0:
            return [
                Reply(
                    text="✍️ Отправьте сумму числом, например 1200.",
                    buttons=(_cancel_row(),),
                    retry_input=True,
                )
            ]
        next_payload = {"step": "category", "amount": str(amounts[0].value)}
        await set_pending(
            settings,
            user_id=actor.user_id,
            workspace_id=workspace_id,
            kind="manual_form",
            payload=next_payload,
        )
        return await _category_prompt(
            settings, actor=actor, workspace=workspace, payload=next_payload
        )
    if step == "category":
        wanted = normalize_name(text)
        skip = wanted in {"пропустить", "нет", "без категории"}
        chosen: tuple[str, str] | None = None
        if not skip:
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                categories = await list_categories(session, workspace_id=workspace_id)
            exact = [item for item in categories if normalize_name(item.name) == wanted]
            starts = [item for item in categories if normalize_name(item.name).startswith(wanted)]
            match = exact[0] if exact else (starts[0] if len(starts) == 1 else None)
            if match is None:
                return await _category_prompt(
                    settings,
                    actor=actor,
                    workspace=workspace,
                    payload=payload,
                    notice=f"⚠️ Категории «{text.strip()[:40]}» нет.",
                )
            chosen = (str(match.id), match.name)
        return await _choose_category(
            settings, actor=actor, workspace=workspace, payload=payload, chosen=chosen
        )
    if step == "date":
        today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
        parsed = resolve_date_expression(text, reference=today)
        if parsed is None:
            return [
                Reply(
                    text="📅 Не узнал дату. Нажмите кнопку или отправьте, например, 20.09.",
                    buttons=_date_prompt(None)[0].buttons,
                    retry_input=True,
                )
            ]
        return await _choose_date(
            settings, actor=actor, workspace=workspace, payload=payload, day=parsed.value
        )
    comment = "" if normalize_name(text) in {"пропустить", "нет", "без комментария"} else text
    return await _finish(settings, actor=actor, workspace=workspace, payload=payload, note=comment)


async def _choose_category(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    payload: dict[str, Any],
    chosen: tuple[str, str] | None,
) -> list[Reply]:
    from fintracker.application.conversation.pending import set_pending

    next_payload = {
        **payload,
        "step": "date",
        "category_id": chosen[0] if chosen else "",
        "category_name": chosen[1] if chosen else "",
    }
    await set_pending(
        settings,
        user_id=actor.user_id,
        workspace_id=actor.require_workspace(),
        kind="manual_form",
        payload=next_payload,
    )
    return _date_prompt(chosen[1] if chosen else "Без категории")


async def _choose_date(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    payload: dict[str, Any],
    day: dt.date,
) -> list[Reply]:
    from fintracker.application.conversation.pending import set_pending

    await set_pending(
        settings,
        user_id=actor.user_id,
        workspace_id=actor.require_workspace(),
        kind="manual_form",
        payload={**payload, "step": "comment", "date": day.isoformat()},
    )
    return _comment_prompt()


async def _finish(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    payload: dict[str, Any],
    note: str,
) -> list[Reply]:
    from fintracker.application.conversation.pending import clear_pending

    category_id = str(payload.get("category_id") or "")
    replies = await post_manual_expense(
        settings,
        actor=actor,
        workspace=workspace,
        amount=Decimal(str(payload.get("amount") or "0")),
        category_id=uuid.UUID(category_id) if category_id else None,
        occurred=dt.date.fromisoformat(str(payload["date"])) if payload.get("date") else None,
        note=note.strip() or None,
    )
    await clear_pending(settings, user_id=actor.user_id, workspace_id=actor.require_workspace())
    return replies


async def form_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Кнопки ручной формы: категория, дата, комментарий, отмена."""
    from fintracker.application.conversation.pending import clear_pending, peek_pending

    workspace_id = actor.require_workspace()
    if action == "cancel":
        await clear_pending(settings, user_id=actor.user_id, workspace_id=workspace_id)
        return [
            Reply(
                text="↩️ Ввод отменён. Ничего не записано.",
                buttons=((Button("🏠 Меню", callback("menu", "main")),),),
            )
        ]
    pending = await peek_pending(settings, user_id=actor.user_id, workspace_id=workspace_id)
    if pending is None or pending.kind != "manual_form":
        return [
            Reply(
                text=(
                    "🔄 Эта форма уже закрыта.\n\n"
                    "Чтобы добавить трату, нажмите «➕ Добавить трату»."
                ),
                buttons=((Button("➕ Добавить трату", callback("menu", "add")),),),
            )
        ]
    payload = dict(pending.payload)
    step = str(payload.get("step") or "")
    if action == "cpage" and step == "category":
        page = int(rest[0]) if rest and rest[0].isdigit() else 0
        return await _category_prompt(
            settings, actor=actor, workspace=workspace, payload=payload, page=page
        )
    if action == "cat" and step == "category" and rest:
        chosen: tuple[str, str] | None = None
        if rest[0] != "-":
            from fintracker.application.catalog.categories import list_categories

            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                categories = await list_categories(session, workspace_id=workspace_id)
            match = next((item for item in categories if short(item.id) == rest[0]), None)
            if match is None:
                return await _category_prompt(
                    settings,
                    actor=actor,
                    workspace=workspace,
                    payload=payload,
                    notice="🔄 Категория недоступна, выберите другую.",
                )
            chosen = (str(match.id), match.name)
        return await _choose_category(
            settings, actor=actor, workspace=workspace, payload=payload, chosen=chosen
        )
    if action == "date" and step == "date" and rest and rest[0].isdigit():
        today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
        day = today - dt.timedelta(days=min(int(rest[0]), 7))
        return await _choose_date(
            settings, actor=actor, workspace=workspace, payload=payload, day=day
        )
    if action == "note" and step == "comment":
        return await _finish(settings, actor=actor, workspace=workspace, payload=payload, note="")
    return [
        Reply(
            text="🔄 Эта кнопка относится к прошлому шагу. Продолжите с текущего вопроса.",
        )
    ]


async def post_manual_expense(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    amount: Decimal,
    category_id: uuid.UUID | None,
    occurred: dt.date | None,
    note: str | None,
) -> list[Reply]:
    """Провести расход формы без модели и пересчитать пороги лимитов."""
    from fintracker.application.delivery.thresholds import refresh_thresholds

    money = Money.from_decimal(amount, workspace.currency)
    if money.minor <= 0:
        raise ValidationFailed("Сумма должна быть больше нуля")
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    spec = TransactionSpec(
        transaction_type=TransactionType.EXPENSE,
        amount=money,
        occurred_date=occurred or today,
        timezone=workspace.timezone,
        note=note,
        allocations=(
            AllocationSpec(role=AllocationRole.EXPENSE, amount=money, category_id=category_id),
        ),
        cash_legs=(CashLegSpec(signed=-money, coverage=CoverageMode.UNKNOWN),),
    )
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        result = await post_transaction(session, uow, actor=actor, spec=spec, origin="form")
        await refresh_thresholds(session, uow, workspace=workspace)

    from fintracker.application.conversation.sections import transaction_card_reply

    return await transaction_card_reply(
        settings,
        actor=actor,
        workspace=workspace,
        transaction_id=result.transaction_id,
        confirmation=True,
    )


async def submit_manual_form(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, text: str
) -> list[Reply]:
    """Строка «сумма | категория | дата | комментарий» без обращения к модели."""
    from fintracker.application.catalog.categories import list_categories
    from fintracker.application.catalog.normalize import normalize_name

    parts = [part.strip() for part in text.split("|")]
    if not parts or not parts[0]:
        raise ValidationFailed("Укажите хотя бы сумму")
    amounts = parse_amounts(parts[0])
    if not amounts:
        raise ValidationFailed("Не удалось разобрать сумму: первой частью должна быть сумма")
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    occurred = today
    if len(parts) > 2 and parts[2]:
        parsed = resolve_date_expression(parts[2], reference=today)
        if parsed is None:
            raise ValidationFailed("Не удалось разобрать дату")
        occurred = parsed.value
    note = parts[3] if len(parts) > 3 and parts[3] else None
    category_id = None
    if len(parts) > 1 and parts[1]:
        wanted = normalize_name(parts[1])
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            categories = await list_categories(session, workspace_id=workspace_id)
        match = next((item for item in categories if normalize_name(item.name) == wanted), None)
        if match is None:
            raise ValidationFailed(f"Категории «{parts[1]}» нет. Создайте её или выберите другую.")
        category_id = match.id
    return await post_manual_expense(
        settings,
        actor=actor,
        workspace=workspace,
        amount=Decimal(amounts[0].value),
        category_id=category_id,
        occurred=occurred,
        note=note,
    )
