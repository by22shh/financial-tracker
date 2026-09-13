"""Периодический анализ и рекомендации (FR-73–FR-76, AI-08, AI-07).

Сервис аналитики вычисляет показатели; модель сопоставляет их с целями,
ранжирует варианты и формулирует объяснение.
Команды: CMD-23 (запуск анализа), CMD-24 (обратная связь), CMD-25 (общий
календарь анализа). Сервер проверяет числа и
допустимость действия: карточка с неподтверждённой суммой не доставляется.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
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
from fintracker.core.errors import ProviderUnavailable, QuotaExceeded, ValidationFailed
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


async def run_analysis(
    settings: Settings,
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    workspace: Workspace,
    run_kind: str,
    logical_key: str,
    today: dt.date,
) -> AnalysisOutcome:
    """Выполнить один логический запуск анализа (FR-73, A128, A129)."""
    existing = (
        await session.execute(
            select(AnalysisRun).where(
                AnalysisRun.workspace_id == workspace.id,
                AnalysisRun.logical_key == logical_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Повтор фоновой задачи не создаёт второй обзор.
        return AnalysisOutcome(
            run_id=existing.id,
            status=existing.status,
            summary="",
            recommendations=(),
            abstained_reason=None,
            fallback_used=existing.fallback_used,
        )

    period = await period_for_date(session, workspace_id=workspace.id, day=today)
    snapshot_row, metrics = await build_snapshot_row(
        session, workspace=workspace, period_id=period.id, today=today
    )
    fingerprint = _fingerprint(metrics)

    previous = (
        await session.execute(
            select(AnalysisRun)
            .where(
                AnalysisRun.workspace_id == workspace.id,
                AnalysisRun.status.in_(("succeeded", "no_new_data")),
            )
            .order_by(AnalysisRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    run = AnalysisRun(
        workspace_id=workspace.id,
        run_kind=run_kind,
        logical_key=logical_key,
        snapshot_id=snapshot_row.id,
        status="running",
        content_fingerprint=fingerprint,
    )
    session.add(run)
    await session.flush()

    if previous is not None and previous.content_fingerprint == fingerprint:
        # Без новых подходящих данных те же советы не отправляются (A129).
        run.status = "no_new_data"
        run.finished_at = dt.datetime.now(dt.UTC)
        await session.flush()
        return AnalysisOutcome(
            run_id=run.id,
            status="no_new_data",
            summary="С прошлого анализа новых подходящих данных не появилось.",
            recommendations=(),
            abstained_reason=None,
            fallback_used=False,
        )

    allowed, coverage_reason = _coverage_allows_recommendations(metrics)
    protected = await _protected_lines(session, workspace_id=workspace.id, period_id=period.id)
    blocked = await _rejected_directions(session, workspace_id=workspace.id, today=today)
    muted = await _muted_directions(session, workspace_id=workspace.id)

    if not allowed:
        run.status = "succeeded"
        run.finished_at = dt.datetime.now(dt.UTC)
        await session.flush()
        summary = fallback_summary(metrics, workspace.currency)
        return AnalysisOutcome(
            run_id=run.id,
            status="succeeded",
            summary=f"{summary}\n{coverage_reason}",
            recommendations=(),
            abstained_reason=coverage_reason,
            fallback_used=False,
        )

    if not settings.ai.enabled:
        run.status = "fallback"
        run.fallback_used = True
        run.finished_at = dt.datetime.now(dt.UTC)
        await session.flush()
        return AnalysisOutcome(
            run_id=run.id,
            status="fallback",
            summary=fallback_summary(metrics, workspace.currency),
            recommendations=(),
            abstained_reason="AI недоступен",
            fallback_used=True,
        )

    request_key = f"analysis:{run.id}"
    try:
        reservation = await quota.reserve(
            settings,
            request_key=request_key,
            upper_bound=upper_bound_cost(
                settings.ai, input_tokens=6000, max_output=settings.ai.max_output_tokens
            ),
            workspace_id=workspace.id,
            purpose="recommendation",
        )
    except QuotaExceeded:
        run.status = "fallback"
        run.fallback_used = True
        run.finished_at = dt.datetime.now(dt.UTC)
        await session.flush()
        return AnalysisOutcome(
            run_id=run.id,
            status="fallback",
            summary=fallback_summary(metrics, workspace.currency),
            recommendations=(),
            abstained_reason="Лимит расходов на AI исчерпан",
            fallback_used=True,
        )

    provider = build_provider(settings.ai)
    try:
        result = await provider.structured(
            instructions=RECOMMENDATION_INSTRUCTIONS,
            input_items=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Числовой снимок бюджета (единственный источник чисел):\n"
                                f"{json.dumps(metrics, ensure_ascii=False, default=str)}\n\n"
                                f"Защищённые статьи: {sorted(protected)}\n"
                                f"Отключённые направления: {sorted(blocked)}"
                            ),
                        }
                    ],
                }
            ],
            response_model=RecommendationResponse,
            prompt_version=RECOMMENDATION_PROMPT_VERSION,
            schema_name="recommendation_v1",
        )
    except (ProviderUnavailable, ValidationFailed) as exc:
        await quota.settle(settings, reservation, actual=None)
        run.status = "fallback"
        run.fallback_used = True
        run.finished_at = dt.datetime.now(dt.UTC)
        await session.flush()
        logger.info("analysis_fallback", reason=type(exc).__name__)
        # Выдуманные рекомендации не подставляются (A138).
        return AnalysisOutcome(
            run_id=run.id,
            status="fallback",
            summary=fallback_summary(metrics, workspace.currency),
            recommendations=(),
            abstained_reason="Генерация недоступна",
            fallback_used=True,
        )

    await quota.settle(settings, reservation, actual=result.cost)
    assert isinstance(result.parsed, RecommendationResponse)
    accepted, rejected = validate_cards(
        result.parsed,
        snapshot=metrics,
        protected_lines=protected,
        blocked_directions=blocked,
        muted=muted,
        currency=workspace.currency,
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

    created: list[uuid.UUID] = []
    for card in accepted:
        row = Recommendation(
            workspace_id=workspace.id,
            run_id=run.id,
            direction=str(card["direction"]),
            observation=str(card["observation"]),
            action_kind=str(card["action_kind"]),
            metric_refs=card["metric_refs"],
            estimated_effect_minor=card["estimated_effect_minor"],
            effect_formula=card["effect_formula"],
            effect_unavailable_reason=card["effect_unavailable_reason"],
            horizon_period_id=period.id,
            conditions=card["conditions"],
            alternative_group=card["alternative_group"],
            stable_line_id=uuid.UUID(str(card["stable_line_id"]))
            if card["stable_line_id"]
            else None,
            revision_vector=snapshot_row.revision_vector,
            priority=int(card["priority"]) if isinstance(card["priority"], int) else 100,
            status="proposed",
        )
        session.add(row)
        await session.flush()
        created.append(row.id)

    await uow.emit(
        workspace_id=workspace.id,
        event_type="AnalysisCompleted",
        aggregate_type="analysis_run",
        aggregate_id=run.id,
        payload={
            "text": result.parsed.summary,
            "run_id": str(run.id),
            "cards": len(created),
        },
    )
    return AnalysisOutcome(
        run_id=run.id,
        status="succeeded",
        summary=result.parsed.summary,
        recommendations=tuple(created),
        abstained_reason=result.parsed.abstained_reason,
        fallback_used=False,
    )


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
