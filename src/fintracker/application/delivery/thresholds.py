"""Пороговые события лимитов (FR-52, A56–A60).

Состояние обновляется вместе с финансовой командой под блокировкой бюджета.
Прыжок 70→105% создаёт только верхнее актуальное предупреждение; возврат или
новая версия плана не сбрасывают историю порогов.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.planning.plan import LimitState, LineStatus, period_status
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.platform import ThresholdEvent
from fintracker.db.uow import UnitOfWork

# Порядок важен: выбирается только самый верхний достигнутый порог (A56).
THRESHOLD_ORDER: tuple[tuple[str, float], ...] = (
    ("overspent", 100.0),
    ("exhausted_100", 100.0),
    ("approach_90", 90.0),
    ("approach_80", 80.0),
)


@dataclass(frozen=True, slots=True)
class ThresholdOutcome:
    stable_line_id: uuid.UUID
    threshold_type: str
    line_name: str
    fact_minor: int
    limit_minor: int
    currency: str

    def message(self, workspace_name: str) -> str:
        fact = Money(self.fact_minor, self.currency).format()
        limit = Money(self.limit_minor, self.currency).format()
        if self.threshold_type == "overspent":
            over = Money(self.fact_minor - self.limit_minor, self.currency).format()
            body = f"Перерасход {over}: потрачено {fact} при плане {limit}"
        elif self.threshold_type == "exhausted_100":
            # При точном равенстве слово «перерасход» не используется (A57).
            body = f"Лимит исчерпан: потрачено {fact} из {limit}"
        else:
            percent = 90 if self.threshold_type == "approach_90" else 80
            remaining = Money(self.limit_minor - self.fact_minor, self.currency).format()
            body = (
                f"Достигнуто {percent}% лимита: потрачено {fact} из {limit}, осталось {remaining}"
            )
        return f"{workspace_name}\n{self.line_name}\n{body}"


def _reached_threshold(line: LineStatus) -> str | None:
    """Определить верхний достигнутый порог строки."""
    if line.limit_state is not LimitState.POSITIVE or line.effective_limit_minor is None:
        # Для незаданного, нулевого и отрицательного лимита пороги не применяются.
        return None
    if line.fact_minor > line.effective_limit_minor:
        return "overspent"
    if line.fact_minor == line.effective_limit_minor:
        return "exhausted_100"
    if line.usage_percent is None:
        return None
    if line.usage_percent >= 90:
        return "approach_90"
    if line.usage_percent >= 80:
        return "approach_80"
    return None


async def evaluate_thresholds(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    workspace: Workspace,
    period_id: uuid.UUID,
    today: dt.date,
) -> list[ThresholdOutcome]:
    """Пересчитать пороги и создать недостающие события.

    Возврат ниже порога и повторное его достижение не создают второе фоновое
    предупреждение в этом периоде (A59). История порогов не очищается при
    изменении плана (A60).
    """
    status = await period_status(
        session,
        workspace_id=workspace.id,
        period_id=period_id,
        currency=workspace.currency,
        today=today,
    )
    existing = {
        (row.stable_line_id, row.threshold_type)
        for row in (
            (
                await session.execute(
                    select(ThresholdEvent).where(
                        ThresholdEvent.workspace_id == workspace.id,
                        ThresholdEvent.period_id == period_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    }

    outcomes: list[ThresholdOutcome] = []
    for line in status.lines:
        threshold = _reached_threshold(line)
        if threshold is None or line.effective_limit_minor is None:
            continue
        if (line.stable_line_id, threshold) in existing:
            continue
        name = line.category_name
        if line.beneficiary_name:
            name = f"{name} · {line.beneficiary_name}"
        statement = (
            pg_insert(ThresholdEvent)
            .values(
                workspace_id=workspace.id,
                period_id=period_id,
                stable_line_id=line.stable_line_id,
                threshold_type=threshold,
                fact_minor=line.fact_minor,
                limit_minor=line.effective_limit_minor,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ThresholdEvent.workspace_id,
                    ThresholdEvent.period_id,
                    ThresholdEvent.stable_line_id,
                    ThresholdEvent.threshold_type,
                ]
            )
            .returning(ThresholdEvent.id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none()
        if inserted is None:
            continue
        outcome = ThresholdOutcome(
            stable_line_id=line.stable_line_id,
            threshold_type=threshold,
            line_name=name,
            fact_minor=line.fact_minor,
            limit_minor=line.effective_limit_minor,
            currency=workspace.currency,
        )
        outcomes.append(outcome)
        event = await uow.emit(
            workspace_id=workspace.id,
            event_type="ThresholdCrossed",
            aggregate_type="budget_line",
            aggregate_id=line.stable_line_id,
            payload={
                "text": outcome.message(workspace.name),
                "threshold_type": threshold,
                "period_id": str(period_id),
            },
        )
        await session.flush()
        await session.execute(
            update(ThresholdEvent).where(ThresholdEvent.id == inserted).values(event_id=event.id)
        )
    return outcomes
