"""Ручная форма ввода, работающая без AI (FR-10, NFR-14, A103)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from fintracker.application.conversation.keyboards import Button, callback
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

FORM_HELP = """Ручной ввод без распознавания. Отправьте строку вида:

сумма | категория | дата | комментарий

Например: 1200 | Продукты | вчера | ужин на выходные
Обязательна только сумма. Дата по умолчанию — сегодня."""


async def start_manual_form(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    from fintracker.application.catalog.categories import list_categories

    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        categories = await list_categories(session, workspace_id=workspace_id)
    names = ", ".join(item.name for item in categories[:12]) or "категорий пока нет"
    return [
        Reply(
            text=f"{FORM_HELP}\n\nДоступные категории: {names}",
            buttons=((Button("Отмена", callback("noop", "x")),),),
        )
    ]


async def submit_manual_form(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, text: str
) -> list[Reply]:
    """Разобрать строку формы и провести операцию без обращения к модели."""
    from fintracker.application.catalog.categories import list_categories
    from fintracker.application.catalog.normalize import normalize_name

    parts = [part.strip() for part in text.split("|")]
    if not parts or not parts[0]:
        raise ValidationFailed("Укажите хотя бы сумму")
    amounts = parse_amounts(parts[0])
    if not amounts:
        raise ValidationFailed("Не удалось разобрать сумму")
    amount = Money.from_decimal(Decimal(amounts[0].value), workspace.currency)

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    occurred = today
    if len(parts) > 2 and parts[2]:
        parsed = resolve_date_expression(parts[2], reference=today)
        if parsed is None:
            raise ValidationFailed("Не удалось разобрать дату")
        occurred = parsed.value
    note = parts[3] if len(parts) > 3 and parts[3] else None

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=actor.correlation_id)
        await uow.lock_workspace(workspace_id, actor=actor)
        category_id = None
        if len(parts) > 1 and parts[1]:
            wanted = normalize_name(parts[1])
            categories = await list_categories(session, workspace_id=workspace_id)
            match = next((item for item in categories if normalize_name(item.name) == wanted), None)
            if match is None:
                raise ValidationFailed(
                    f"Категория «{parts[1]}» не найдена. Создайте её или выберите другую."
                )
            category_id = match.id

        spec = TransactionSpec(
            transaction_type=TransactionType.EXPENSE,
            amount=amount,
            occurred_date=occurred,
            timezone=workspace.timezone,
            note=note,
            allocations=(
                AllocationSpec(role=AllocationRole.EXPENSE, amount=amount, category_id=category_id),
            ),
            cash_legs=(CashLegSpec(signed=-amount, coverage=CoverageMode.UNKNOWN),),
        )
        result = await post_transaction(session, uow, actor=actor, spec=spec, origin="form")

    from fintracker.application.conversation.sections import transaction_card_reply

    return await transaction_card_reply(
        settings, actor=actor, workspace=workspace, transaction_id=result.transaction_id
    )
