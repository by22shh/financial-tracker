"""Автоматическое открытие периода и перенос остатка (FR-92, FR-93, FR-39).

Открытие не зависит от AI, подтверждения предложения и наличия открытого чата.
Задача повторяема безопасно: для одной границы создаётся один период и не более
одного исходного плана по принятому шаблону.
"""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.planning.periods import ensure_periods
from fintracker.application.planning.plan import (
    PlanLineSpec,
    create_budget_version,
    current_budget_version,
    line_key,
)
from fintracker.application.platform import queue
from fintracker.application.platform.queue import LeasedJob
from fintracker.config import Settings
from fintracker.core.errors import NotFound
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Workspace
from fintracker.db.models.planning import (
    BudgetLine,
    BudgetPeriod,
    BudgetVersion,
    RecurringPlanTemplate,
    Rollover,
)
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("planning.rollover")


async def active_template(
    session: AsyncSession, *, workspace_id: uuid.UUID, on_date: dt.date
) -> RecurringPlanTemplate | None:
    """Версия шаблона, действовавшая на границу периода (FR-93, A214)."""
    return (
        await session.execute(
            select(RecurringPlanTemplate)
            .where(
                RecurringPlanTemplate.workspace_id == workspace_id,
                RecurringPlanTemplate.effective_from <= on_date,
            )
            .order_by(
                RecurringPlanTemplate.effective_from.desc(),
                RecurringPlanTemplate.version.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def _template_lines(template: RecurringPlanTemplate) -> list[PlanLineSpec]:
    specs: list[PlanLineSpec] = []
    for raw in template.lines:
        if not isinstance(raw, dict) or "category_id" not in raw:
            continue
        specs.append(
            PlanLineSpec(
                category_id=uuid.UUID(str(raw["category_id"])),
                beneficiary_id=(
                    uuid.UUID(str(raw["beneficiary_id"])) if raw.get("beneficiary_id") else None
                ),
                limit_minor=raw.get("limit_minor"),
                rollover_mode=str(raw.get("rollover_mode", "none")),
                is_protected=bool(raw.get("is_protected", False)),
            )
        )
    return specs


async def apply_plan_for_period(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    workspace_id: uuid.UUID,
    period: BudgetPeriod,
) -> BudgetVersion | None:
    """Создать исходный план нового периода (FR-93).

    Порядок: уже утверждённый индивидуальный план → действующая версия
    включённого шаблона → черновик без утверждённых лимитов.
    """
    existing = await current_budget_version(session, workspace_id=workspace_id, period_id=period.id)
    if existing is not None:
        # Индивидуально утверждённый план не перезаписывается шаблоном (FR-93).
        return existing

    template = await active_template(session, workspace_id=workspace_id, on_date=period.start_date)
    if template is None or not template.enabled:
        # Отсутствие принятого лимита не подменяется нулём (A221).
        version = await create_budget_version(
            session,
            workspace_id=workspace_id,
            period_id=period.id,
            kind="working",
            plan_status="draft",
            origin="manual",
            lines=[],
            reason="Повторение плана отключено: проект не утверждён",
        )
        return version

    lines = _template_lines(template)
    baseline = await create_budget_version(
        session,
        workspace_id=workspace_id,
        period_id=period.id,
        kind="baseline",
        plan_status="approved",
        origin="template",
        lines=lines,
        overall_limit_minor=template.overall_limit_minor,
        template_version=template.version,
        approved_by=template.approval_actor_id,
        reason="Перенесён из утверждённого шаблона",
    )
    working = await create_budget_version(
        session,
        workspace_id=workspace_id,
        period_id=period.id,
        kind="working",
        plan_status="approved",
        origin="template",
        lines=lines,
        overall_limit_minor=template.overall_limit_minor,
        template_version=template.version,
        approved_by=template.approval_actor_id,
        reason="Перенесён из утверждённого шаблона",
    )
    logger.info(
        "plan_from_template",
        workspace_id=str(workspace_id),
        period_id=str(period.id),
        template_version=template.version,
        baseline_version=baseline.version,
    )
    return working


async def propose_rollovers(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    closed_period: BudgetPeriod,
    next_period: BudgetPeriod,
    currency: str,
    today: dt.date,
) -> list[Rollover]:
    """Сформировать перенос остатка при закрытии периода (FR-39, B5).

    Для неполного периода перенос остаётся предложением до явного принятия;
    повтор задачи не создаёт второй перенос.
    """
    from fintracker.application.planning.plan import period_status

    version = await current_budget_version(
        session, workspace_id=workspace_id, period_id=closed_period.id
    )
    if version is None:
        return []
    lines = (
        (
            await session.execute(
                select(BudgetLine).where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.budget_version_id == version.id,
                    BudgetLine.rollover_mode != "none",
                )
            )
        )
        .scalars()
        .all()
    )
    if not lines:
        return []

    status = await period_status(
        session,
        workspace_id=workspace_id,
        period_id=closed_period.id,
        currency=currency,
        today=today,
    )
    by_key = {line_key(line.category_id, line.beneficiary_id): line for line in status.lines}
    auto_accept = closed_period.completeness == "confirmed_complete"
    created: list[Rollover] = []
    for line in lines:
        key = line_key(line.category_id, line.beneficiary_id)
        line_status = by_key.get(key)
        if line_status is None or line_status.remaining_minor is None:
            continue
        remaining = line_status.remaining_minor
        if line.rollover_mode == "positive_only" and remaining <= 0:
            continue
        if remaining == 0:
            continue
        existing = (
            await session.execute(
                select(Rollover).where(
                    Rollover.workspace_id == workspace_id,
                    Rollover.source_period_id == closed_period.id,
                    Rollover.destination_period_id == next_period.id,
                    Rollover.stable_line_id == line.stable_line_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        row = Rollover(
            workspace_id=workspace_id,
            source_period_id=closed_period.id,
            destination_period_id=next_period.id,
            stable_line_id=line.stable_line_id,
            amount_minor=remaining,
            mode=line.rollover_mode,
            status="accepted" if auto_accept else "proposed",
            basis_completeness=closed_period.completeness,
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


def plan_review_lead_days(period_days: int) -> int:
    """Опережение обзора плана в днях (FORM-10, FR-52).

    ``min(3, max(0, длительность − 1))``: для месяца и двух недель это три дня,
    для однодневного периода опережение равно нулю и обзор объединяется с
    сообщением об открытии следующего периода (A228).
    """
    return min(3, max(0, period_days - 1))


def plan_review_date(*, start_date: dt.date, end_exclusive: dt.date) -> dt.date:
    """Дата обзора плана внутри границ текущего периода (FR-52)."""
    days = (end_exclusive - start_date).days
    lead = plan_review_lead_days(days)
    review = end_exclusive - dt.timedelta(days=lead + 1)
    # Задание не ставится раньше начала текущего периода.
    return max(start_date, review)


async def handle_open_next_period(settings: Settings, job: LeasedJob) -> None:
    """Обработчик задачи открытия периода (FR-92, A149, A212–A214)."""
    workspace_id = job.workspace_id
    if workspace_id is None:
        raise NotFound("У задачи открытия периода нет бюджета")
    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        uow = UnitOfWork(session=session, correlation_id=job.correlation_id)
        workspace = await uow.lock_workspace(workspace_id)
        today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
        # Задание описывает границу, на которую было поставлено; задержка
        # обработки не должна терять пропущенные границы (FR-92, A149, A214).
        raw_local_date = job.payload.get("local_date")
        if isinstance(raw_local_date, str):
            today = max(today, dt.date.fromisoformat(raw_local_date))

        await ensure_periods(session, workspace_id=workspace_id, until_date=today)

        # Полная инициализация периода не зависит от того, кто первым создал
        # его строку: чтение бюджета материализует календарь, поэтому задача
        # доводит до конца все периоды без плана (FR-92, FR-93, G-05).
        periods = (
            (
                await session.execute(
                    select(BudgetPeriod)
                    .where(
                        BudgetPeriod.workspace_id == workspace_id,
                        BudgetPeriod.start_date <= today,
                    )
                    .order_by(BudgetPeriod.start_date)
                )
            )
            .scalars()
            .all()
        )
        pending: list[BudgetPeriod] = []
        for row in periods:
            plan = await current_budget_version(
                session, workspace_id=workspace_id, period_id=row.id
            )
            closing = row.end_exclusive <= today and row.state != "ended"
            if plan is None or closing:
                pending.append(row)
        if not pending:
            return

        created = pending
        # Предшественник берётся по календарю, а не по тому, что было до вызова.
        by_start = {row.start_date: row for row in periods}
        starts = sorted(by_start)
        previous: BudgetPeriod | None = None
        for period in pending:
            index = starts.index(period.start_date)
            previous = by_start[starts[index - 1]] if index > 0 else None
            await apply_plan_for_period(session, uow, workspace_id=workspace_id, period=period)
            if (
                previous is not None
                and previous.state != "ended"
                and previous.end_exclusive <= today
            ):
                previous.state = "ended"
                # Календарное закрытие не подтверждает полноту истории (FR-39).
                previous.closed_at = dt.datetime.now(dt.UTC)
                await session.flush()
                await propose_rollovers(
                    session,
                    workspace_id=workspace_id,
                    closed_period=previous,
                    next_period=period,
                    currency=workspace.currency,
                    today=today,
                )
                await uow.emit(
                    workspace_id=workspace_id,
                    event_type="BudgetPeriodEnded",
                    aggregate_type="budget_period",
                    aggregate_id=previous.id,
                    payload={
                        "period_id": str(previous.id),
                        "completeness": previous.completeness,
                    },
                )
            # Обзор плана: для однодневного периода он совмещается с открытием
            # следующего и отдельным заданием не ставится (FORM-10, A228).
            review_on = plan_review_date(
                start_date=period.start_date, end_exclusive=period.end_exclusive
            )
            if plan_review_lead_days((period.end_exclusive - period.start_date).days) > 0:
                await queue.enqueue(
                    session,
                    job_type="plan_review",
                    logical_key=f"plan_review:{workspace_id}:{period.id}",
                    queue_class="calendar",
                    workspace_id=workspace_id,
                    payload={
                        "period_id": str(period.id),
                        "review_date": review_on.isoformat(),
                        "schema_version": 1,
                    },
                    available_at=dt.datetime.combine(
                        review_on, dt.time(9, 0), tzinfo=ZoneInfo(workspace.timezone)
                    ).astimezone(dt.UTC),
                    correlation_id=job.correlation_id,
                )
            await uow.emit(
                workspace_id=workspace_id,
                event_type="BudgetPeriodOpened",
                aggregate_type="budget_period",
                aggregate_id=period.id,
                payload={
                    "period_id": str(period.id),
                    "start_date": period.start_date.isoformat(),
                    "end_inclusive": (period.end_exclusive - dt.timedelta(days=1)).isoformat(),
                },
            )

        await uow.bump_revisions(workspace_id, calendar=True, plan=True)
        logger.info(
            "periods_opened",
            workspace_id=str(workspace_id),
            count=len(created),
            until=today.isoformat(),
        )


async def ensure_current_period(
    settings: Settings, *, workspace_id: uuid.UUID, correlation_id: str = ""
) -> None:
    """Материализовать календарь при чтении/записи (FR-92).

    Задержка фонового исполнителя не должна относить покупку к старому периоду.
    """
    async with session_scope(settings, RuntimeRole.API, workspace_id=workspace_id) as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one_or_none()
        if workspace is None:
            return
        today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
        uow = UnitOfWork(session=session, correlation_id=correlation_id)
        created = await ensure_periods(session, workspace_id=workspace_id, until_date=today)
        for materialized in created:
            period = (
                await session.execute(
                    select(BudgetPeriod).where(
                        BudgetPeriod.workspace_id == workspace_id,
                        BudgetPeriod.id == materialized.id,
                    )
                )
            ).scalar_one()
            await apply_plan_for_period(session, uow, workspace_id=workspace_id, period=period)


async def handle_plan_review(settings: Settings, job: LeasedJob) -> None:
    """Обзор плана перед границей периода (FR-52, FORM-10).

    Обзор показывает состояние и предложения, но не меняет суммы сам.
    """
    workspace_id = job.workspace_id
    if workspace_id is None:
        raise NotFound("У задачи обзора плана нет бюджета")
    period_id = uuid.UUID(str(job.payload["period_id"]))
    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        uow = UnitOfWork(session=session, correlation_id=job.correlation_id)
        workspace = await uow.lock_workspace(workspace_id)
        period = (
            await session.execute(
                select(BudgetPeriod).where(
                    BudgetPeriod.workspace_id == workspace_id, BudgetPeriod.id == period_id
                )
            )
        ).scalar_one_or_none()
        if period is None or period.state == "ended":
            # Устаревшее задание после смены календаря не выполняется (FR-52).
            return
        today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
        if today >= period.end_exclusive:
            return
        await uow.emit(
            workspace_id=workspace_id,
            event_type="PlanReviewDue",
            aggregate_type="budget_period",
            aggregate_id=period_id,
            payload={
                "period_id": str(period_id),
                "end_inclusive": (period.end_exclusive - dt.timedelta(days=1)).isoformat(),
                "schema_version": 1,
            },
        )
