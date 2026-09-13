"""Периодический анализ и рекомендации (FR-73–FR-76, AI-08, AI-07).

Сервис аналитики вычисляет показатели; модель сопоставляет их с целями,
ранжирует варианты и формулирует объяснение.
Команды: CMD-23 (запуск анализа), CMD-24 (обратная связь), CMD-25 (общий
календарь анализа). Сервер проверяет числа и
допустимость действия: карточка с неподтверждённой суммой не доставляется.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import period_snapshot_metrics
from fintracker.application.intelligence import quota
from fintracker.application.intelligence.prompts import (
    RECOMMENDATION_INSTRUCTIONS,
    RECOMMENDATION_PROMPT_VERSION,
)
from fintracker.application.planning.periods import period_for_date
from fintracker.config import Settings
from fintracker.core.errors import (
    DomainError,
    ProviderUnavailable,
    QuotaExceeded,
    TemporarilyUnavailable,
    ValidationFailed,
)
from fintracker.core.fencing import get_execution_identity
from fintracker.core.logging import get_logger
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.intelligence import (
    AnalysisRun,
    AnalyticsSnapshot,
    DirectionMute,
    Recommendation,
    RecommendationFeedback,
)
from fintracker.db.models.planning import BudgetLine, BudgetVersion
from fintracker.db.models.platform import AICostReservation, Job
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.infra.ai.openai_client import build_provider, upper_bound_cost
from fintracker.infra.ai.schemas import RecommendationResponse

logger = get_logger("intelligence.analysis")


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    run_id: uuid.UUID
    status: str
    summary: str
    recommendations: tuple[uuid.UUID, ...]
    abstained_reason: str | None
    fallback_used: bool


def _fingerprint(snapshot: dict[str, object]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def fallback_summary(snapshot: dict[str, object], currency: str) -> str:
    """Числовая сводка по шаблону при недоступности генерации (AI-08, A93, A138)."""
    period = snapshot.get("period", {})
    assert isinstance(period, dict)
    raw_fact = snapshot.get("total_fact_minor", 0)
    total_fact = int(raw_fact) if isinstance(raw_fact, int) else 0
    total_limit = snapshot.get("total_limit_minor")
    lines = [
        f"Период: {period.get('start')} — {period.get('end_inclusive')}",
        f"Учтённые расходы: {Money(total_fact, currency).format()}",
    ]
    if isinstance(total_limit, int):
        lines.append(f"План расходов: {Money(total_limit, currency).format()}")
        lines.append(f"Осталось по плану: {Money(total_limit - total_fact, currency).format()}")
    else:
        lines.append("План расходов: не задан")
    raw_lines = snapshot.get("lines", [])
    assert isinstance(raw_lines, list)
    over = [
        item
        for item in raw_lines
        if isinstance(item, dict)
        and isinstance(item.get("limit_minor"), int)
        and int(item["fact_minor"]) > int(item["limit_minor"])
    ]
    if over:
        lines.append(f"Превышено статей: {len(over)}")
    coverage = snapshot.get("coverage")
    if coverage == "incomplete":
        lines.append("Полнота учёта не подтверждена: выводы ограничены.")
    lines.append("Рекомендации не сформированы: доступна только числовая сводка.")
    return "\n".join(lines)


def _coverage_allows_recommendations(snapshot: dict[str, object]) -> tuple[bool, str | None]:
    """Неполная история ограничивает выводы (FR-73, A130, B9)."""
    if snapshot.get("coverage") == "incomplete":
        return False, (
            "Полнота учёта не подтверждена: частота покупок, средний чек и "
            "прогноз экономии по этим данным не выводятся"
        )
    period = snapshot.get("period", {})
    assert isinstance(period, dict)
    if int(period.get("observed_days", 0) or 0) < 7:
        return False, "Наблюдаемых дней меньше семи: темп расходов не рассчитывается"
    return True, None


async def build_snapshot_row(
    session: AsyncSession,
    *,
    workspace: Workspace,
    period_id: uuid.UUID,
    today: dt.date,
) -> tuple[AnalyticsSnapshot, dict[str, object]]:
    """Сохранить числовой снимок с вектором версий основы (ADR-09)."""
    metrics = await period_snapshot_metrics(
        session, workspace=workspace, period_id=period_id, today=today
    )
    period = metrics["period"]
    assert isinstance(period, dict)
    row = AnalyticsSnapshot(
        workspace_id=workspace.id,
        period_id=period_id,
        date_from=dt.date.fromisoformat(str(period["start"])),
        date_to_exclusive=dt.date.fromisoformat(str(period["end_inclusive"]))
        + dt.timedelta(days=1),
        filter_hash=_fingerprint({"period": period_id}),
        method="period_status_v1",
        revision_vector={
            "data": metrics["data_revision"],
            "plan": metrics["plan_revision"],
            "calendar": metrics["calendar_revision"],
            "catalog": metrics["catalog_revision"],
            "coverage": metrics["coverage_revision"],
        },
        coverage_status=str(metrics["coverage"]),
        metrics=metrics,
    )
    session.add(row)
    await session.flush()
    return row, metrics


async def _muted_directions(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> set[tuple[str, str | None]]:
    rows = (
        await session.execute(
            select(DirectionMute.direction, DirectionMute.stable_line_id).where(
                DirectionMute.workspace_id == workspace_id
            )
        )
    ).all()
    return {(row[0], str(row[1]) if row[1] else None) for row in rows}


async def _rejected_directions(
    session: AsyncSession, *, workspace_id: uuid.UUID, today: dt.date
) -> set[str]:
    """Отклонённые и отложенные направления не повторяются (FR-76, A134)."""
    rows = (
        await session.execute(
            select(
                Recommendation.direction,
                RecommendationFeedback.decision,
                RecommendationFeedback.snooze_until,
            )
            .join(
                RecommendationFeedback,
                (RecommendationFeedback.workspace_id == Recommendation.workspace_id)
                & (RecommendationFeedback.recommendation_id == Recommendation.id),
            )
            .where(Recommendation.workspace_id == workspace_id)
        )
    ).all()
    blocked: set[str] = set()
    for direction, decision, snooze_until in rows:
        if decision == "rejected":
            blocked.add(direction)
        if decision == "snoozed" and snooze_until and snooze_until > today:
            blocked.add(direction)
    return blocked


async def _protected_lines(
    session: AsyncSession, *, workspace_id: uuid.UUID, period_id: uuid.UUID
) -> set[str]:
    """Защищённые статьи не предлагаются к сокращению (FR-74, A94)."""
    version = (
        await session.execute(
            select(BudgetVersion)
            .where(
                BudgetVersion.workspace_id == workspace_id,
                BudgetVersion.period_id == period_id,
                BudgetVersion.kind == "working",
            )
            .order_by(BudgetVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if version is None:
        return set()
    rows = (
        (
            await session.execute(
                select(BudgetLine.stable_line_id).where(
                    BudgetLine.workspace_id == workspace_id,
                    BudgetLine.budget_version_id == version.id,
                    BudgetLine.is_protected.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    return {str(item) for item in rows}


def validate_cards(
    response: RecommendationResponse,
    *,
    snapshot: dict[str, object],
    protected_lines: set[str],
    blocked_directions: set[str],
    muted: set[tuple[str, str | None]],
    currency: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Проверить карточки сервером (AI-08, FR-75).

    Отклоняются карточки без основания, с метрикой вне снимка, с
    выдуманной суммой эффекта и предложения по защищённым статьям.
    """
    metric_ids: set[str] = {str(snapshot.get("metric_id"))}
    raw_lines = snapshot.get("lines", [])
    assert isinstance(raw_lines, list)
    line_ids: dict[str, dict[str, object]] = {}
    for item in raw_lines:
        assert isinstance(item, dict)
        metric_ids.add(str(item["metric_id"]))
        line_ids[str(item["metric_id"])] = item

    accepted: list[dict[str, object]] = []
    rejected: list[str] = []
    for card in response.cards:
        if card.direction in blocked_directions:
            rejected.append(f"{card.direction}: направление отклонено участником")
            continue
        if not card.metric_refs:
            rejected.append(f"{card.direction}: нет ссылки на показатель снимка")
            continue
        unknown = [ref for ref in card.metric_refs if ref not in metric_ids]
        if unknown:
            rejected.append(f"{card.direction}: показатели вне снимка {unknown}")
            continue
        if card.stable_line_id and card.stable_line_id in protected_lines:
            rejected.append(f"{card.direction}: статья защищена")
            continue
        if (card.direction, card.stable_line_id) in muted or (
            card.direction,
            None,
        ) in muted:
            rejected.append(f"{card.direction}: направление отключено для статьи")
            continue
        effect_minor: int | None = None
        if card.estimated_effect_decimal is not None:
            if not card.effect_formula:
                rejected.append(f"{card.direction}: эффект без формулы расчёта")
                continue
            effect_minor = Money.from_decimal(
                Decimal(card.estimated_effect_decimal), currency
            ).minor
            if effect_minor <= 0:
                rejected.append(f"{card.direction}: неположительный эффект")
                continue
        elif not card.effect_unavailable_reason:
            rejected.append(f"{card.direction}: нет ни эффекта, ни объяснения")
            continue
        accepted.append(
            {
                "direction": card.direction,
                "observation": card.observation,
                "action_kind": card.action_kind,
                "metric_refs": list(card.metric_refs),
                "estimated_effect_minor": effect_minor,
                "effect_formula": card.effect_formula,
                "effect_unavailable_reason": card.effect_unavailable_reason,
                "conditions": list(card.conditions),
                "alternative_group": card.alternative_group,
                "stable_line_id": card.stable_line_id,
                "priority": card.priority,
            }
        )
    return accepted, rejected


@dataclass(frozen=True, slots=True)
class _Preparation:
    """Сохранённое задание анализа: всё нужное модели уже зафиксировано."""

    run_id: uuid.UUID
    attempt_id: uuid.UUID
    previous_attempt_id: uuid.UUID | None
    workspace_id: uuid.UUID
    currency: str
    period_id: uuid.UUID
    snapshot_id: uuid.UUID
    revision_vector: dict[str, Any]
    metrics: dict[str, object]
    protected: set[str]
    blocked: set[str]
    muted: set[tuple[str, str | None]]


async def _outcome(session: AsyncSession, run: AnalysisRun) -> AnalysisOutcome:
    cards = tuple(
        (
            await session.scalars(
                select(Recommendation.id).where(
                    Recommendation.workspace_id == run.workspace_id, Recommendation.run_id == run.id
                )
            )
        ).all()
    )
    return AnalysisOutcome(
        run.id, run.status, run.summary, cards, run.abstained_reason, run.fallback_used
    )


async def _publish(session: AsyncSession, uow: UnitOfWork, run: AnalysisRun) -> None:
    """One publication, persisted result and expansion job in the same commit."""
    if run.status not in {"succeeded", "fallback"} or run.published_at is not None:
        return
    from fintracker.application.platform import queue

    cards = (await _outcome(session, run)).recommendations
    await uow.emit(
        workspace_id=run.workspace_id,
        event_type="AnalysisCompleted",
        aggregate_type="analysis_run",
        aggregate_id=run.id,
        payload={"text": run.summary, "run_id": str(run.id), "cards": len(cards)},
    )
    await queue.enqueue(
        session,
        job_type="expand_outbox",
        logical_key=f"expand:analysis:{run.id}",
        queue_class="interactive",
        workspace_id=run.workspace_id,
        payload={"batch": 50, "schema_version": 1},
        correlation_id=uow.correlation_id,
    )
    run.published_at = await uow.now()


async def _attempt_is_live(session: AsyncSession, run: AnalysisRun, now: dt.datetime) -> bool:
    if run.attempt_id is None:
        return False
    if run.attempt_job_id is not None:
        job = await session.get(Job, run.attempt_job_id)
        return bool(
            job is not None
            and job.state == "running"
            and job.lease_token == run.attempt_lease_token
            and job.lease_until is not None
            and job.lease_until > now
        )
    return run.attempt_expires_at is not None and run.attempt_expires_at > now


async def _checked_run(
    session: AsyncSession, preparation: _Preparation, correlation_id: str = ""
) -> tuple[UnitOfWork, AnalysisRun]:
    uow = UnitOfWork(session=session, correlation_id=correlation_id)
    await uow.lock_workspace(preparation.workspace_id)
    run = await session.get(AnalysisRun, preparation.run_id)
    if run is None or run.status != "running" or run.attempt_id != preparation.attempt_id:
        raise TemporarilyUnavailable("Попытка анализа заменена другим исполнителем")
    # Direct (non-job) calls have their own persisted execution deadline.
    if not await _attempt_is_live(session, run, await uow.now()):
        raise TemporarilyUnavailable("Право на сохранение анализа истекло")
    return uow, run


async def _prepare_analysis(
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    run_kind: str,
    logical_key: str,
    today: dt.date,
    correlation_id: str = "",
) -> tuple[_Preparation | None, AnalysisOutcome | None]:
    """Короткая транзакция подготовки: снимок и задание сохраняются (R-07).

    Возвращает либо задание для модели, либо готовый результат, если модель
    не нужна: повтор, отсутствие новых данных, неполнота учёта, выключенный AI.
    """
    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        uow = UnitOfWork(session=session, correlation_id=correlation_id)
        await uow.lock_workspace(workspace_id)
        now = await uow.now()
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        existing = (
            await session.execute(
                select(AnalysisRun).where(
                    AnalysisRun.workspace_id == workspace_id,
                    AnalysisRun.logical_key == logical_key,
                )
            )
        ).scalar_one_or_none()
        previous_attempt_id = existing.attempt_id if existing is not None else None
        if existing is not None and existing.status not in {"running", "pending"}:
            if (
                existing.status in {"succeeded", "fallback"}
                and existing.published_at is None
                and not existing.summary
            ):
                # Older versions did not persist coverage/fallback summaries.
                # Recover a numerical report from that run's original snapshot.
                snapshot = await session.get(AnalyticsSnapshot, existing.snapshot_id)
                if snapshot is None:
                    raise TemporarilyUnavailable("Исходный снимок анализа недоступен")
                existing.summary = fallback_summary(snapshot.metrics, workspace.currency)
                existing.abstained_reason = "Исходный текст старой сводки не был сохранён"
                existing.status = "fallback"
                existing.fallback_used = True
            await _publish(session, uow, existing)
            return None, await _outcome(session, existing)
        if (
            existing is not None
            and existing.status == "running"
            and await _attempt_is_live(session, existing, now)
        ):
            raise TemporarilyUnavailable("Анализ уже выполняется")

        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        snapshot_row, metrics = await build_snapshot_row(
            session, workspace=workspace, period_id=period.id, today=today
        )
        fingerprint = _fingerprint(metrics)
        previous = (
            await session.execute(
                select(AnalysisRun)
                .where(
                    AnalysisRun.workspace_id == workspace_id,
                    AnalysisRun.status.in_(("succeeded", "no_new_data")),
                )
                .order_by(AnalysisRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        run = existing or AnalysisRun(
            workspace_id=workspace_id,
            run_kind=run_kind,
            logical_key=logical_key,
            snapshot_id=snapshot_row.id,
            status="running",
            content_fingerprint=fingerprint,
        )
        session.add(run)
        run.snapshot_id = snapshot_row.id
        run.content_fingerprint = fingerprint
        run.status = "running"
        await session.flush()
        currency = workspace.currency

        if previous is not None and previous.content_fingerprint == fingerprint:
            # Без новых подходящих данных те же советы не отправляются (A129).
            run.status = "no_new_data"
            run.finished_at = dt.datetime.now(dt.UTC)
            run.summary = "С прошлого анализа новых подходящих данных не появилось."
            return None, AnalysisOutcome(
                run_id=run.id,
                status="no_new_data",
                summary="С прошлого анализа новых подходящих данных не появилось.",
                recommendations=(),
                abstained_reason=None,
                fallback_used=False,
            )

        allowed, coverage_reason = _coverage_allows_recommendations(metrics)
        if not allowed:
            run.status = "succeeded"
            run.finished_at = dt.datetime.now(dt.UTC)
            summary = fallback_summary(metrics, currency)
            run.summary = f"{summary}\n{coverage_reason}"
            run.abstained_reason = coverage_reason
            await _publish(session, uow, run)
            return None, await _outcome(session, run)

        if not settings.ai.enabled:
            run.status = "fallback"
            run.fallback_used = True
            run.finished_at = dt.datetime.now(dt.UTC)
            run.summary = fallback_summary(metrics, currency)
            run.abstained_reason = "AI недоступен"
            await _publish(session, uow, run)
            return None, await _outcome(session, run)

        run.attempt_id = uuid.uuid4()
        run.attempt_expires_at = now + dt.timedelta(
            seconds=settings.ai.request_timeout_seconds + 30
        )
        identity = get_execution_identity()
        run.attempt_job_id = identity[0] if identity else None
        run.attempt_lease_token = identity[1] if identity else None
        preparation = _Preparation(
            run_id=run.id,
            attempt_id=run.attempt_id,
            previous_attempt_id=previous_attempt_id,
            workspace_id=workspace_id,
            currency=currency,
            period_id=period.id,
            snapshot_id=snapshot_row.id,
            revision_vector=dict(snapshot_row.revision_vector),
            metrics=metrics,
            protected=await _protected_lines(
                session, workspace_id=workspace_id, period_id=period.id
            ),
            blocked=await _rejected_directions(session, workspace_id=workspace_id, today=today),
            muted=await _muted_directions(session, workspace_id=workspace_id),
        )
    return preparation, None


async def _mark_fallback(
    settings: Settings, preparation: _Preparation, *, reason: str
) -> AnalysisOutcome:
    """Отметить запуск как выполненный по шаблону (AI-08, A138)."""
    async with session_scope(
        settings, RuntimeRole.WORKER, workspace_id=preparation.workspace_id
    ) as session:
        uow, run = await _checked_run(session, preparation)
        run.status = "fallback"
        run.fallback_used = True
        run.finished_at = await uow.now()
        run.summary = fallback_summary(preparation.metrics, preparation.currency)
        run.abstained_reason = reason
        await _publish(session, uow, run)
    return AnalysisOutcome(
        run_id=preparation.run_id,
        status="fallback",
        summary=fallback_summary(preparation.metrics, preparation.currency),
        recommendations=(),
        abstained_reason=reason,
        fallback_used=True,
    )


async def _generate_cards(settings: Settings, preparation: _Preparation) -> Any:
    """Вызов модели вне транзакции базы (ADR-04, AUD-07, R-07)."""
    provider = build_provider(settings.ai)
    return await provider.structured(
        instructions=RECOMMENDATION_INSTRUCTIONS,
        input_items=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Числовой снимок бюджета (единственный источник чисел):\n"
                            f"{json.dumps(preparation.metrics, ensure_ascii=False, default=str)}"
                            f"\n\nЗащищённые статьи: {sorted(preparation.protected)}\n"
                            f"Отключённые направления: {sorted(preparation.blocked)}"
                        ),
                    }
                ],
            }
        ],
        response_model=RecommendationResponse,
        prompt_version=RECOMMENDATION_PROMPT_VERSION,
        schema_name="recommendation_v1",
    )


async def _store_analysis(
    settings: Settings, preparation: _Preparation, result: Any, *, correlation_id: str
) -> AnalysisOutcome:
    """Короткая транзакция сохранения проверенных карточек (FR-75, R-07)."""
    async with session_scope(
        settings, RuntimeRole.WORKER, workspace_id=preparation.workspace_id
    ) as session:
        uow, run = await _checked_run(session, preparation, correlation_id)
        assert isinstance(result.parsed, RecommendationResponse)
        accepted, rejected = validate_cards(
            result.parsed,
            snapshot=preparation.metrics,
            protected_lines=preparation.protected,
            blocked_directions=preparation.blocked,
            muted=preparation.muted,
            currency=preparation.currency,
        )
        if rejected:
            logger.info("recommendations_rejected", count=len(rejected), reasons=rejected[:3])

        run.status = "succeeded"
        run.profile_version = result.profile_version
        run.requested_model = result.requested_model
        run.returned_model = result.returned_model
        run.reasoning_effort = result.reasoning_effort
        run.service_tier = result.service_tier
        run.prompt_version = result.prompt_version
        run.schema_version = result.schema_version
        run.usage = result.usage.as_dict()
        run.cost_amount = result.cost
        run.cost_currency = result.cost_currency
        run.provider_request_id = result.provider_request_id
        run.finished_at = dt.datetime.now(dt.UTC)
        run.summary = result.parsed.summary
        run.abstained_reason = result.parsed.abstained_reason

        created: list[uuid.UUID] = []
        for card in accepted:
            row = Recommendation(
                workspace_id=preparation.workspace_id,
                run_id=run.id,
                direction=str(card["direction"]),
                observation=str(card["observation"]),
                action_kind=str(card["action_kind"]),
                metric_refs=card["metric_refs"],
                estimated_effect_minor=card["estimated_effect_minor"],
                effect_formula=card["effect_formula"],
                effect_unavailable_reason=card["effect_unavailable_reason"],
                horizon_period_id=preparation.period_id,
                conditions=card["conditions"],
                alternative_group=card["alternative_group"],
                stable_line_id=uuid.UUID(str(card["stable_line_id"]))
                if card["stable_line_id"]
                else None,
                revision_vector=preparation.revision_vector,
                priority=int(card["priority"]) if isinstance(card["priority"], int) else 100,
                status="proposed",
            )
            session.add(row)
            await session.flush()
            created.append(row.id)

        await _publish(session, uow, run)
    return AnalysisOutcome(
        run_id=preparation.run_id,
        status="succeeded",
        summary=result.parsed.summary,
        recommendations=tuple(created),
        abstained_reason=result.parsed.abstained_reason,
        fallback_used=False,
    )


async def run_analysis(
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    run_kind: str,
    logical_key: str,
    today: dt.date,
    correlation_id: str = "",
) -> AnalysisOutcome:
    """Один логический запуск анализа (FR-73, A128, A129, R-07).

    Три стадии с короткими транзакциями: подготовка снимка и задания, вызов
    модели вне транзакции базы, сохранение проверенных карточек. Долгий ответ
    провайдера не удерживает соединение и не рвёт транзакцию (ADR-04).
    """
    preparation, outcome = await _prepare_analysis(
        settings,
        workspace_id=workspace_id,
        run_kind=run_kind,
        logical_key=logical_key,
        today=today,
        correlation_id=correlation_id,
    )
    if preparation is None:
        assert outcome is not None
        # Recovery may finish without a model (AI disabled, changed coverage or
        # no new data). Its crashed provider request must still release a slot.
        async with session_scope(
            settings, RuntimeRole.WORKER, workspace_id=workspace_id
        ) as session:
            attempt_id = await session.scalar(
                select(AnalysisRun.attempt_id).where(AnalysisRun.id == outcome.run_id)
            )
        if attempt_id is not None:
            await _settle_abandoned(settings, outcome.run_id, attempt_id)
        return outcome

    # A crashed request may have incurred cost. Preserve its budget reservation,
    # but release its concurrency slot before acquiring a fresh attempt's slot.
    request_key = f"analysis:{preparation.run_id}:{preparation.attempt_id}"
    reservation: quota.Reservation | None = None
    try:
        if preparation.previous_attempt_id is not None:
            await _settle_abandoned(settings, preparation.run_id, preparation.previous_attempt_id)
        reservation = await quota.reserve(
            settings,
            request_key=request_key,
            upper_bound=upper_bound_cost(
                settings.ai, input_tokens=6000, max_output=settings.ai.max_output_tokens
            ),
            workspace_id=workspace_id,
            purpose="recommendation",
        )
        try:
            # Deadline bounds direct calls too; no transaction spans this await.
            result = await asyncio.wait_for(
                _generate_cards(settings, preparation), timeout=settings.ai.request_timeout_seconds
            )
        except (ProviderUnavailable, ValidationFailed, TimeoutError) as exc:
            await quota.settle(settings, reservation, actual=None)
            logger.info("analysis_fallback", reason=type(exc).__name__)
            return await _mark_fallback(settings, preparation, reason="Генерация недоступна")
        await quota.settle(settings, reservation, actual=result.cost)
        return await _store_analysis(settings, preparation, result, correlation_id=correlation_id)
    except QuotaExceeded:
        return await _mark_fallback(settings, preparation, reason="Лимит расходов на AI исчерпан")
    except BaseException:
        # Shield only cleanup: cancellation still propagates to the worker.
        # A hard process crash is recovered via persisted attempt owner/deadline.
        await asyncio.shield(_abandon_attempt(settings, preparation, reservation))
        raise


async def _settle_abandoned(settings: Settings, run_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
    async with session_scope(settings, RuntimeRole.WORKER) as session:
        row = (
            await session.scalars(
                select(AICostReservation).where(
                    AICostReservation.request_key == f"analysis:{run_id}:{attempt_id}"
                )
            )
        ).one_or_none()
        reservation = (
            quota.Reservation(
                row.request_key,
                row.reserved_amount,
                row.currency,
                row.quota_month,
                row.workspace_id,
            )
            if row is not None
            else None
        )
    if reservation is not None:
        await quota.settle(settings, reservation, actual=None)


async def _abandon_attempt(
    settings: Settings, preparation: _Preparation, reservation: quota.Reservation | None
) -> None:
    if reservation is not None:
        await quota.settle(settings, reservation, actual=None)
    else:
        # Cancellation may arrive after reserve committed but before its return
        # value was assigned to the caller.
        await _settle_abandoned(settings, preparation.run_id, preparation.attempt_id)
    try:
        async with session_scope(
            settings, RuntimeRole.WORKER, workspace_id=preparation.workspace_id
        ) as session:
            _, run = await _checked_run(session, preparation)
            run.status = "pending"
            run.attempt_expires_at = None
    except DomainError:
        # Do not alter another owner's state or write into a quarantined budget.
        # The next valid owner detects the stale job/deadline and resumes.
        logger.info("analysis_abandon_fenced", run_id=str(preparation.run_id))


async def mark_stale_recommendations(session: AsyncSession, *, workspace: Workspace) -> int:
    """Пометить устаревшие предложения после изменения основы (FR-76, A133)."""
    rows = (
        (
            await session.execute(
                select(Recommendation).where(
                    Recommendation.workspace_id == workspace.id,
                    Recommendation.status == "proposed",
                )
            )
        )
        .scalars()
        .all()
    )
    current = {
        "data": workspace.data_revision,
        "plan": workspace.plan_revision,
        "calendar": workspace.calendar_revision,
        "catalog": workspace.catalog_revision,
        "coverage": workspace.coverage_revision,
    }
    stale = 0
    for row in rows:
        vector = dict(row.revision_vector)
        # Изменение заметки не сбрасывает сверку, но денежная правка делает
        # текстовую рекомендацию неактуальной (ADR-09).
        if any(current.get(key) != value for key, value in vector.items()):
            row.status = "stale"
            stale += 1
    await session.flush()
    return stale


async def schedule_weekly_analysis(
    session: AsyncSession, *, workspace: Workspace, today: dt.date
) -> str:
    """Логический ключ недельного обзора: один результат на запуск (A128)."""
    local_today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    iso_year, iso_week, _ = (today or local_today).isocalendar()
    return f"weekly:{workspace.id}:{iso_year}-{iso_week:02d}"
