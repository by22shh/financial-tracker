"""Резервирование стоимости AI и квоты (ADR-12, H12, AR-29).

Месячный лимит расходов на AI и деградация после его исчерпания (LIM-11).
До запроса короткая транзакция атомарно резервирует верхнюю оценку стоимости.
Фактическая стоимость закрывает резервацию, остаток освобождается. При
неизвестном результате сумма остаётся зарезервированной до сверки; повторный
вызов требует новой резервации.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings
from fintracker.core.errors import QuotaExceeded
from fintracker.core.logging import get_logger
from fintracker.db.models.platform import AICostReservation, AIQuotaCounter
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("intelligence.quota")


def quota_month(now: dt.datetime | None = None) -> str:
    """Расчётное окно квоты — календарный месяц UTC (ADR-12)."""
    moment = now or dt.datetime.now(dt.UTC)
    return moment.astimezone(dt.UTC).strftime("%Y-%m")


@dataclass(frozen=True, slots=True)
class Reservation:
    request_key: str
    reserved: Decimal
    currency: str
    month: str
    workspace_id: uuid.UUID | None


async def _ensure_counter(
    session: AsyncSession,
    *,
    scope: str,
    workspace_id: uuid.UUID | None,
    month: str,
    limit: Decimal,
    currency: str,
) -> AIQuotaCounter:
    statement = (
        pg_insert(AIQuotaCounter)
        .values(
            scope=scope,
            workspace_id=workspace_id,
            quota_month=month,
            limit_amount=limit,
            currency=currency,
        )
        .on_conflict_do_nothing(
            index_elements=[
                AIQuotaCounter.scope,
                AIQuotaCounter.workspace_id,
                AIQuotaCounter.quota_month,
            ]
        )
    )
    await session.execute(statement)
    row = (
        await session.execute(
            select(AIQuotaCounter)
            .where(
                AIQuotaCounter.scope == scope,
                AIQuotaCounter.quota_month == month,
                AIQuotaCounter.workspace_id == workspace_id
                if workspace_id is not None
                else AIQuotaCounter.workspace_id.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one()
    return row


async def reserve(
    settings: Settings,
    *,
    request_key: str,
    upper_bound: Decimal,
    workspace_id: uuid.UUID | None,
    purpose: str,
) -> Reservation:
    """Зарезервировать верхнюю оценку стоимости до вызова провайдера.

    Порядок блокировок: строка общего сервисного лимита → строка квоты
    бюджета → reservation. Эти блокировки не удерживаются одновременно с
    финансовым workspace lock (ADR-12).
    """
    month = quota_month()
    currency = settings.ai.cost_currency
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        existing = (
            await session.execute(
                select(AICostReservation).where(AICostReservation.request_key == request_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Повторный вызов требует новой резервации (ADR-12).
            raise QuotaExceeded(
                "Для этого запроса уже есть резервация: создайте новый ключ запроса"
            )

        global_counter = await _ensure_counter(
            session,
            scope="global",
            workspace_id=None,
            month=month,
            limit=settings.ai.monthly_cost_limit,
            currency=currency,
        )
        committed = global_counter.settled_total + global_counter.reserved_total
        if committed + upper_bound > global_counter.limit_amount:
            raise QuotaExceeded(
                "Месячный лимит расходов на AI исчерпан. Ручной учёт, отчёты, "
                "исправления и экспорт продолжают работать."
            )
        if global_counter.in_flight >= settings.ai.max_concurrent_interactive:
            raise QuotaExceeded("Достигнут предел одновременных обращений к модели")
        global_counter.reserved_total = global_counter.reserved_total + upper_bound
        global_counter.in_flight += 1

        if workspace_id is not None:
            workspace_counter = await _ensure_counter(
                session,
                scope="workspace",
                workspace_id=workspace_id,
                month=month,
                limit=settings.ai.monthly_cost_limit,
                currency=currency,
            )
            if workspace_counter.in_flight >= settings.ai.max_concurrent_per_workspace:
                raise QuotaExceeded("В этом бюджете уже выполняются обращения к модели, подождите")
            ws_committed = workspace_counter.settled_total + workspace_counter.reserved_total
            if ws_committed + upper_bound > workspace_counter.limit_amount:
                raise QuotaExceeded("Месячный лимит расходов бюджета на AI исчерпан")
            workspace_counter.reserved_total = workspace_counter.reserved_total + upper_bound
            workspace_counter.in_flight += 1

        session.add(
            AICostReservation(
                service_scope="global",
                workspace_id=workspace_id,
                quota_month=month,
                request_key=request_key,
                purpose=purpose,
                reserved_amount=upper_bound,
                currency=currency,
                state="reserved",
            )
        )
    return Reservation(
        request_key=request_key,
        reserved=upper_bound,
        currency=currency,
        month=month,
        workspace_id=workspace_id,
    )


async def settle(settings: Settings, reservation: Reservation, *, actual: Decimal | None) -> None:
    """Закрыть резервацию фактической стоимостью.

    ``actual=None`` означает неизвестный результат: сумма остаётся
    зарезервированной до сверки с провайдером (ADR-12, AR-29).
    """
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        row = (
            await session.execute(
                select(AICostReservation)
                .where(AICostReservation.request_key == reservation.request_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.state != "reserved":
            return

        global_counter = await _ensure_counter(
            session,
            scope="global",
            workspace_id=None,
            month=reservation.month,
            limit=settings.ai.monthly_cost_limit,
            currency=reservation.currency,
        )
        global_counter.in_flight = max(0, global_counter.in_flight - 1)
        workspace_counter = None
        if reservation.workspace_id is not None:
            workspace_counter = await _ensure_counter(
                session,
                scope="workspace",
                workspace_id=reservation.workspace_id,
                month=reservation.month,
                limit=settings.ai.monthly_cost_limit,
                currency=reservation.currency,
            )
            workspace_counter.in_flight = max(0, workspace_counter.in_flight - 1)

        if actual is None:
            row.state = "unknown"
            logger.warning("ai_cost_unknown", request_key=reservation.request_key)
            return

        row.state = "settled"
        row.actual_amount = actual
        row.settled_at = dt.datetime.now(dt.UTC)
        global_counter.reserved_total = max(
            Decimal(0), global_counter.reserved_total - reservation.reserved
        )
        global_counter.settled_total = global_counter.settled_total + actual
        if workspace_counter is not None:
            workspace_counter.reserved_total = max(
                Decimal(0), workspace_counter.reserved_total - reservation.reserved
            )
            workspace_counter.settled_total = workspace_counter.settled_total + actual


async def monthly_usage(
    settings: Settings, *, workspace_id: uuid.UUID | None = None
) -> tuple[Decimal, Decimal]:
    """Потрачено и предел за текущий месяц — для предупреждения о лимите."""
    month = quota_month()
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        row = (
            await session.execute(
                select(AIQuotaCounter).where(
                    AIQuotaCounter.scope == ("workspace" if workspace_id else "global"),
                    AIQuotaCounter.quota_month == month,
                    AIQuotaCounter.workspace_id == workspace_id
                    if workspace_id is not None
                    else AIQuotaCounter.workspace_id.is_(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return Decimal(0), settings.ai.monthly_cost_limit
    return row.settled_total + row.reserved_total, row.limit_amount
