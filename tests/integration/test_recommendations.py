"""Периодический анализ и рекомендации A128–A140 (FR-73–FR-76, AI-08)."""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.intelligence.analysis import (
    build_snapshot_row,
    fallback_summary,
    mark_stale_recommendations,
    run_analysis,
    validate_cards,
)
from fintracker.application.ledger.service import post_transaction
from fintracker.config import Settings
from fintracker.db.models.intelligence import (
    AnalysisRun,
    Recommendation,
)
from fintracker.db.models.planning import BudgetLine
from fintracker.infra.ai.openai_client import ScriptedAIProvider, set_provider_override
from fintracker.infra.ai.schemas import RecommendationResponse
from tests.conftest import requires_pg
from tests.integration.factories import build_fixture
from tests.integration.test_money_scenarios import expense_spec, rub

pytestmark = [pytest.mark.pg, requires_pg]

TODAY = dt.date(2026, 9, 25)


@pytest.fixture
def ai_settings(test_settings: Settings) -> Settings:
    previous = {
        key: os.environ.get(key) for key in ("FINTRACKER_AI__ENABLED", "FINTRACKER_AI__API_KEY")
    }
    os.environ["FINTRACKER_AI__ENABLED"] = "true"
    os.environ["FINTRACKER_AI__API_KEY"] = "test-key"
    settings = Settings()
    yield settings
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def recommendation_json(**overrides) -> str:
    card = {
        "direction": "flexible_spend",
        "observation": "Расходы на рестораны растут быстрее плана",
        "action_kind": "reduce_flexible",
        "metric_refs": [],
        "estimated_effect_decimal": "1100.00",
        "effect_formula": "2 × (900 − 350)",
        "effect_unavailable_reason": None,
        "conditions": ["Известны четыре доставки по 900 ₽"],
        "alternative_group": None,
        "stable_line_id": None,
        "priority": 10,
    }
    card.update(overrides.pop("card", {}))
    payload = {
        "schema_version": "1.0",
        "summary": "Недельный обзор",
        "abstained_reason": None,
        "cards": [card],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


async def _complete_fixture(session: AsyncSession, **kwargs):
    """Бюджет с подтверждённой полнотой и расходами для анализа."""
    fixture = await build_fixture(session, limits={"Рестораны": 1_000_000}, **kwargs)
    fixture.period.completeness = "confirmed_complete"
    await session.flush()
    await post_transaction(
        session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(
            fixture, amount=rub(9_000), category="Рестораны", occurred=dt.date(2026, 9, 15)
        ),
        origin="form",
    )
    # Анализ выполняется отдельными короткими транзакциями (R-07), поэтому
    # подготовленные данные должны быть видимы другим соединениям.
    await session.commit()
    return fixture


async def test_a128_repeated_task_creates_one_run(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """CMD-23, A128: доставленная дважды задача даёт один логический обзор."""
    fixture = await _complete_fixture(owner_session)
    provider = ScriptedAIProvider(responses=[recommendation_json()])
    set_provider_override(provider)
    try:
        first = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:test",
            today=TODAY,
        )
        second = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:test",
            today=TODAY,
        )
    finally:
        set_provider_override(None)
    assert first.run_id == second.run_id
    runs = (
        (
            await owner_session.execute(
                select(AnalysisRun).where(AnalysisRun.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    assert len(provider.calls) == 1, "модель вызвана один раз"


async def test_a129_no_new_data_does_not_repeat_advice(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A129: при неизменных данных те же советы не отправляются повторно."""
    fixture = await _complete_fixture(owner_session)
    provider = ScriptedAIProvider(responses=[recommendation_json(), recommendation_json()])
    set_provider_override(provider)
    try:
        await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:1",
            today=TODAY,
        )
        second = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:2",
            today=TODAY,
        )
    finally:
        set_provider_override(None)
    assert second.status == "no_new_data"
    assert second.recommendations == ()
    assert len(provider.calls) == 1


async def test_a130_incomplete_history_blocks_recommendations(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A130/B9: неполная история не даёт выдуманной частоты и экономии."""
    fixture = await build_fixture(owner_session, limits={"Рестораны": 1_000_000})
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(9_000), category="Рестораны"),
        origin="form",
    )
    await owner_session.commit()
    provider = ScriptedAIProvider(responses=[recommendation_json()])
    set_provider_override(provider)
    try:
        outcome = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:incomplete",
            today=TODAY,
        )
    finally:
        set_provider_override(None)
    assert outcome.recommendations == ()
    assert outcome.abstained_reason is not None
    assert "Полнота учёта не подтверждена" in outcome.abstained_reason
    assert not provider.calls, "модель не вызывалась при неполной истории"


async def test_a138_generation_failure_gives_numeric_summary(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A138: при сбое генерации доступна числовая сводка без выдуманных советов."""
    from fintracker.core.errors import ProviderUnavailable

    fixture = await _complete_fixture(owner_session)
    provider = ScriptedAIProvider(fail_with=ProviderUnavailable("нет связи"))
    set_provider_override(provider)
    try:
        outcome = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:fail",
            today=TODAY,
        )
    finally:
        set_provider_override(None)
    assert outcome.fallback_used
    assert outcome.recommendations == ()
    assert "Учтённые расходы" in outcome.summary
    assert "Рекомендации не сформированы" in outcome.summary


async def test_a93_no_ai_key_keeps_report(
    clean_db: None, test_settings: Settings, owner_session: AsyncSession
) -> None:
    """A93: без ключа AI числовая сводка доставляется без потери отчёта."""
    fixture = await _complete_fixture(owner_session)
    outcome = await run_analysis(
        test_settings,
        workspace_id=fixture.workspace.id,
        run_kind="weekly_review",
        logical_key="weekly:nokey",
        today=TODAY,
    )
    assert outcome.fallback_used
    assert "Период:" in outcome.summary


async def test_ai08_card_without_metric_reference_is_rejected(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """ADR-09, AI-06, AI-08: карточка без ссылки на показатель снимка не доставляется."""
    fixture = await _complete_fixture(owner_session)
    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    response = RecommendationResponse.model_validate_json(recommendation_json())
    accepted, rejected = validate_cards(
        response,
        snapshot=metrics,
        protected_lines=set(),
        blocked_directions=set(),
        muted=set(),
        currency="RUB",
    )
    assert accepted == []
    assert any("нет ссылки на показатель" in reason for reason in rejected)


async def test_ai08_unknown_metric_and_missing_formula_rejected(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """AI-08: показатель вне снимка и сумма без формулы отклоняются."""
    fixture = await _complete_fixture(owner_session)
    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    unknown = RecommendationResponse.model_validate_json(
        recommendation_json(card={"metric_refs": ["line:выдуманная"]})
    )
    accepted, rejected = validate_cards(
        unknown,
        snapshot=metrics,
        protected_lines=set(),
        blocked_directions=set(),
        muted=set(),
        currency="RUB",
    )
    assert accepted == []
    assert any("вне снимка" in reason for reason in rejected)

    valid_metric = str(metrics["metric_id"])
    no_formula = RecommendationResponse.model_validate_json(
        recommendation_json(card={"metric_refs": [valid_metric], "effect_formula": None})
    )
    accepted, rejected = validate_cards(
        no_formula,
        snapshot=metrics,
        protected_lines=set(),
        blocked_directions=set(),
        muted=set(),
        currency="RUB",
    )
    assert accepted == []
    assert any("без формулы" in reason for reason in rejected)


async def test_a94_protected_line_is_not_offered_for_cut(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A94: защищённая статья не предлагается к сокращению."""
    fixture = await _complete_fixture(owner_session)
    from fintracker.application.planning.plan import current_budget_version

    version = await current_budget_version(
        owner_session, workspace_id=fixture.workspace.id, period_id=fixture.period.id
    )
    assert version is not None
    stable = (
        await owner_session.execute(
            select(BudgetLine.stable_line_id).where(
                BudgetLine.workspace_id == fixture.workspace.id,
                BudgetLine.budget_version_id == version.id,
                BudgetLine.category_id == fixture.categories["Рестораны"],
            )
        )
    ).scalar_one()
    await owner_session.execute(
        BudgetLine.__table__.update()
        .where(BudgetLine.budget_version_id == version.id)
        .values(is_protected=True)
    )
    await owner_session.flush()

    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    response = RecommendationResponse.model_validate_json(
        recommendation_json(
            card={
                "metric_refs": [str(metrics["metric_id"])],
                "stable_line_id": str(stable),
            }
        )
    )
    accepted, rejected = validate_cards(
        response,
        snapshot=metrics,
        protected_lines={str(stable)},
        blocked_directions=set(),
        muted=set(),
        currency="RUB",
    )
    assert accepted == []
    assert any("защищена" in reason for reason in rejected)


async def test_a135_alternatives_share_group(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A135: перекрывающиеся варианты помечаются одной группой альтернатив."""
    fixture = await _complete_fixture(owner_session)
    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    metric = str(metrics["metric_id"])
    payload = json.loads(recommendation_json())
    payload["cards"] = [
        {
            **payload["cards"][0],
            "metric_refs": [metric],
            "alternative_group": "restaurants",
        },
        {
            **payload["cards"][0],
            "metric_refs": [metric],
            "alternative_group": "restaurants",
            "estimated_effect_decimal": "800.00",
        },
    ]
    response = RecommendationResponse.model_validate(payload)
    accepted, _ = validate_cards(
        response,
        snapshot=metrics,
        protected_lines=set(),
        blocked_directions=set(),
        muted=set(),
        currency="RUB",
    )
    assert len(accepted) == 2
    groups = {card["alternative_group"] for card in accepted}
    assert groups == {"restaurants"}, "эффекты помечены как альтернативы"


async def test_a136_unknown_price_gives_card_without_effect(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A136: при неизвестной цене карточка идёт без выдуманной суммы эффекта."""
    fixture = await _complete_fixture(owner_session)
    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    response = RecommendationResponse.model_validate_json(
        recommendation_json(
            card={
                "metric_refs": [str(metrics["metric_id"])],
                "estimated_effect_decimal": None,
                "effect_formula": None,
                "effect_unavailable_reason": "Стоимость альтернативы неизвестна",
            }
        )
    )
    accepted, _rejected = validate_cards(
        response,
        snapshot=metrics,
        protected_lines=set(),
        blocked_directions=set(),
        muted=set(),
        currency="RUB",
    )
    assert len(accepted) == 1
    assert accepted[0]["estimated_effect_minor"] is None
    assert accepted[0]["effect_unavailable_reason"]


async def test_a133_correction_marks_recommendation_stale(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A133, AR-26: исправление делает непринятое предложение устаревшим.

    Поздний ответ анализа на устаревшей основе не выполняется: карточка
    получает статус stale, а не применяется молча.
    """
    fixture = await _complete_fixture(owner_session)
    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    provider = ScriptedAIProvider(
        responses=[recommendation_json(card={"metric_refs": [str(metrics["metric_id"])]})]
    )
    set_provider_override(provider)
    try:
        outcome = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:stale",
            today=TODAY,
        )
    finally:
        set_provider_override(None)
    assert len(outcome.recommendations) == 1

    # Новая трата меняет вектор версий основы.
    await post_transaction(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        spec=expense_spec(fixture, amount=rub(500), category="Рестораны"),
        origin="form",
    )
    await owner_session.refresh(fixture.workspace)
    stale = await mark_stale_recommendations(owner_session, workspace=fixture.workspace)
    assert stale == 1
    statuses = (
        (
            await owner_session.execute(
                select(Recommendation.status).where(
                    Recommendation.workspace_id == fixture.workspace.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert statuses == ["stale"]


async def test_a134_muted_direction_is_not_offered(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """CMD-25, A134: отключённое направление не предлагается, лимиты продолжают работать."""
    fixture = await _complete_fixture(owner_session)
    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    response = RecommendationResponse.model_validate_json(
        recommendation_json(card={"metric_refs": [str(metrics["metric_id"])]})
    )
    accepted, rejected = validate_cards(
        response,
        snapshot=metrics,
        protected_lines=set(),
        blocked_directions={"flexible_spend"},
        muted=set(),
        currency="RUB",
    )
    assert accepted == []
    assert any("отклонено участником" in reason for reason in rejected)


def test_fallback_summary_marks_incomplete_coverage() -> None:
    """B9: неполный период не используется как полный месяц."""
    snapshot = {
        "period": {"start": "2026-08-10", "end_inclusive": "2026-09-09", "observed_days": 16},
        "total_fact_minor": 500_000,
        "total_limit_minor": 900_000,
        "coverage": "incomplete",
        "lines": [],
    }
    summary = fallback_summary(snapshot, "RUB")
    assert "Полнота учёта не подтверждена" in summary
    assert "Учтённые расходы: 5 000,00 ₽" in summary


async def test_a131_alternative_effect_names_conditions_and_horizon(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A131: условный эффект замены снабжён горизонтом и основанием."""
    fixture = await _complete_fixture(owner_session)
    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    card = {
        "metric_refs": [str(metrics["metric_id"])],
        "observation": "Две доставки по 900 ₽ за неделю",
        "estimated_effect_decimal": "1100.00",
        "effect_formula": "2 × (900 − 350) ₽ за оставшиеся недели периода",
        "conditions": ["Сохраняется прежняя частота", "Альтернатива доступна по 350 ₽"],
    }
    provider = ScriptedAIProvider(responses=[recommendation_json(card=card)])
    set_provider_override(provider)
    try:
        outcome = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:a131",
            today=TODAY,
        )
    finally:
        set_provider_override(None)

    assert len(outcome.recommendations) == 1
    row = (
        await owner_session.execute(
            select(Recommendation).where(Recommendation.workspace_id == fixture.workspace.id)
        )
    ).scalar_one()
    assert row.estimated_effect_minor == 110_000, "эффект переведён в minor units сервером"
    assert row.effect_formula, "расчёт эффекта указан"
    assert row.conditions, "условия расчёта перечислены"
    assert row.horizon_period_id == fixture.period.id, "горизонт указан"


async def test_a140_unknown_subscription_is_not_claimed_detected(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A140: P0 опирается на подтверждённые расписания, подписка не «обнаружена»."""
    from fintracker.application.commitments.schedules import (
        create_schedule,
        materialize_occurrences,
        upcoming_payments,
    )
    from fintracker.core.money import Money
    from fintracker.domain.schedule import ScheduleKind, ScheduleRule

    fixture = await _complete_fixture(owner_session)
    await create_schedule(
        owner_session,
        fixture.uow,
        actor=fixture.actor,
        name="Интернет",
        direction="payment",
        rule=ScheduleRule(
            kind=ScheduleKind.MONTHLY,
            anchor_date=dt.date(2026, 9, 20),
            interval=1,
            day_of_month=20,
        ),
        currency="RUB",
        expected=Money(90_000, "RUB"),
    )
    await materialize_occurrences(
        owner_session, workspace_id=fixture.workspace.id, until_date=dt.date(2026, 10, 31)
    )
    confirmed = await upcoming_payments(
        owner_session,
        workspace_id=fixture.workspace.id,
        today=TODAY,
        horizon_days=40,
        currency="RUB",
    )
    assert {item.schedule_name for item in confirmed} == {"Интернет"}
    confirmed_count = len(confirmed)

    # Повторяющиеся траты без расписания не превращаются в обязательство.
    for offset in range(3):
        await post_transaction(
            owner_session,
            fixture.uow,
            actor=fixture.actor,
            spec=expense_spec(
                fixture,
                amount=rub(500),
                category="Рестораны",
                occurred=TODAY - dt.timedelta(days=offset * 7),
            ),
            origin="form",
        )
    still = await upcoming_payments(
        owner_session,
        workspace_id=fixture.workspace.id,
        today=TODAY,
        horizon_days=40,
        currency="RUB",
    )
    assert len(still) == confirmed_count, "неизвестная подписка не считается обнаруженной"


async def test_a179_one_run_gives_independent_deliveries(
    clean_db: None, ai_settings: Settings, owner_session: AsyncSession
) -> None:
    """A179: один AI результат на бюджет и независимые доставки участникам."""
    import uuid as _uuid

    from fintracker.application.delivery.dispatch import expand_event
    from fintracker.core.context import MembershipStatus, Role
    from fintracker.core.ids import new_generation
    from fintracker.db.models.access import Membership, User
    from fintracker.db.models.intelligence import AnalysisRun
    from fintracker.db.models.platform import NotificationDelivery, OutboxEvent
    from fintracker.db.session import RuntimeRole, session_scope

    fixture = await _complete_fixture(owner_session, telegram_user_id=5601)
    others = []
    for telegram_id in (5602, 5603):
        user = User(id=_uuid.uuid4(), telegram_user_id=telegram_id)
        owner_session.add(user)
        await owner_session.flush()
        owner_session.add(
            Membership(
                workspace_id=fixture.workspace.id,
                user_id=user.id,
                role=Role.MEMBER.value,
                status=MembershipStatus.ACTIVE.value,
                generation=new_generation(),
            )
        )
        others.append(user)
    await owner_session.flush()

    _, metrics = await build_snapshot_row(
        owner_session, workspace=fixture.workspace, period_id=fixture.period.id, today=TODAY
    )
    provider = ScriptedAIProvider(
        responses=[recommendation_json(card={"metric_refs": [str(metrics["metric_id"])]})]
    )
    set_provider_override(provider)
    try:
        first = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:a179",
            today=TODAY,
        )
        second = await run_analysis(
            ai_settings,
            workspace_id=fixture.workspace.id,
            run_kind="weekly_review",
            logical_key="weekly:a179",
            today=TODAY,
        )
    finally:
        set_provider_override(None)

    assert first.run_id == second.run_id, "один результат на логический запуск"
    assert len(provider.calls) == 1, "повтор не обращается к модели заново"

    runs = (
        (
            await owner_session.execute(
                select(AnalysisRun).where(AnalysisRun.workspace_id == fixture.workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    await owner_session.commit()

    async with session_scope(
        ai_settings, RuntimeRole.OWNER, workspace_id=fixture.workspace.id
    ) as session:
        events = (
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.event_type == "AnalysisCompleted")
                )
            )
            .scalars()
            .all()
        )
        created = 0
        for event in events:
            created += (await expand_event(session, ai_settings, event)).created
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.delivery_class == "review"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert created == 3, "каждому участнику своя доставка"
    assert {row.recipient_user_id for row in deliveries} == {
        fixture.user.id,
        *(user.id for user in others),
    }
