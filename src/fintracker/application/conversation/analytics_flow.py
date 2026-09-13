"""Аналитика по запросу и рекомендации в чате (FR-57–FR-62, FR-73–FR-76)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fintracker.application.analytics.reports import (
    build_forecast,
    format_report,
    spending_report,
)
from fintracker.application.conversation.context import current_status
from fintracker.application.conversation.keyboards import Button, callback, short
from fintracker.application.conversation.types import Reply
from fintracker.application.conversation.views import money
from fintracker.application.planning.periods import period_for_date
from fintracker.config import Settings
from fintracker.core.context import ActorContext
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Workspace
from fintracker.db.models.intelligence import Recommendation, RecommendationFeedback
from fintracker.db.session import RuntimeRole, session_scope

logger = get_logger("conversation.analytics")


async def report_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Отчёт текущего периода с явными границами и полнотой."""
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    status = await current_status(settings, actor=actor, workspace=workspace)
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        report = await spending_report(
            session,
            workspace=workspace,
            date_from=status.start_date,
            date_to_exclusive=status.end_inclusive + dt.timedelta(days=1),
            coverage=status.completeness,
        )
    observed_days = max(1, (min(today, status.end_inclusive) - status.start_date).days + 1)
    flexible_fact = sum(line.fact_minor for line in status.lines if not line.is_protected)
    forecast = build_forecast(
        status=status,
        today=today,
        observed_days=observed_days,
        flexible_fact_minor=flexible_fact,
        coverage=status.completeness,
    )
    lines = [format_report(report)]
    lines.append("")
    lines.append("Прогноз итога периода:")
    lines.append(f"• Факт: {money(forecast.fact_minor, workspace.currency)}")
    lines.append(
        f"• Неисполненные обязательства: {money(forecast.commitments_minor, workspace.currency)}"
    )
    if forecast.flexible_forecast_minor is not None:
        lines.append(
            f"• Прогноз гибких трат: {money(forecast.flexible_forecast_minor, workspace.currency)}"
        )
    if forecast.total_minor is not None:
        lines.append(f"Прогноз: {money(forecast.total_minor, workspace.currency)}")
    else:
        lines.append("Числовой прогноз не строится: данных пока недостаточно.")
    lines.append("Ограничения: " + "; ".join(forecast.limitations))
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("Рекомендации", callback("rec", "list")),
                    Button("Категории", callback("menu", "categories")),
                ),
                (
                    Button("Обзор недели", callback("menu", "review")),
                    Button("Итог периода", callback("menu", "summary")),
                ),
                (Button("← Меню", callback("menu", "main")),),
            ),
        )
    ]


async def answer_question(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, question: str
) -> list[Reply]:
    """Ответ на вопрос: числа считает сервис аналитики (FR-58, AI-06).

    При неоднозначном разрезе бот уточняет, а не смешивает роли автора,
    совершившего покупку и получателя.
    """
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    lowered = question.lower()

    if "я потратил" in lowered or "я потратила" in lowered:
        # «Сколько я потратил» допускает разные смыслы (FR-58).
        return [
            Reply(
                text=(
                    "Уточните разрез: показать записи, которые добавили вы, "
                    "покупки, совершённые вами, или расходы, предназначенные вам?"
                ),
                buttons=(
                    (
                        Button("Я записал", callback("rep", "actor")),
                        Button("Я потратил", callback("rep", "spender")),
                    ),
                    (Button("Для меня", callback("rep", "beneficiary")),),
                ),
            )
        ]

    calendar_month = "календарн" in lowered
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        if calendar_month:
            date_from = today.replace(day=1)
            next_month = (date_from + dt.timedelta(days=32)).replace(day=1)
            date_to_exclusive = next_month
            method_note = "календарный месяц"
        else:
            date_from = period.start_date
            date_to_exclusive = period.end_exclusive
            method_note = "текущий бюджетный период"
        report = await spending_report(
            session,
            workspace=workspace,
            date_from=date_from,
            date_to_exclusive=date_to_exclusive,
            coverage="incomplete",
        )
    header = f"Разрез: {method_note}"
    return [
        Reply(
            text=f"{header}\n{format_report(report)}",
            buttons=(
                (
                    Button("Детализация", callback("menu", "history")),
                    Button("Календарный месяц", callback("rep", "calendar")),
                ),
            ),
        )
    ]


async def recommendation_action(
    settings: Settings,
    *,
    actor: ActorContext,
    workspace: Workspace,
    action: str,
    rest: list[str],
) -> list[Reply]:
    """Список рекомендаций и обратная связь (FR-76)."""
    workspace_id = actor.require_workspace()
    if action == "list":
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            rows = (
                (
                    await session.execute(
                        select(Recommendation)
                        .where(
                            Recommendation.workspace_id == workspace_id,
                            Recommendation.status == "proposed",
                        )
                        .order_by(Recommendation.priority)
                        .limit(3)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return [
                Reply(
                    text=(
                        "Готовых рекомендаций пока нет. Анализ выполняется по "
                        "расписанию и при подготовке следующего плана."
                    ),
                    buttons=((Button("← Меню", callback("menu", "main")),),),
                )
            ]
        replies: list[Reply] = []
        groups_seen: set[str] = set()
        for row in rows:
            body = [row.observation]
            if row.estimated_effect_minor is not None:
                body.append(
                    f"Ожидаемый эффект: {money(row.estimated_effect_minor, workspace.currency)}"
                )
                if row.effect_formula:
                    body.append(f"Расчёт: {row.effect_formula}")
            elif row.effect_unavailable_reason:
                body.append(f"Эффект не рассчитан: {row.effect_unavailable_reason}")
            if row.conditions:
                body.append("Условия: " + "; ".join(str(item) for item in row.conditions))
            if row.alternative_group and row.alternative_group in groups_seen:
                body.append("Это альтернатива предыдущему варианту, эффекты не складываются.")
            if row.alternative_group:
                groups_seen.add(row.alternative_group)
            code = short(row.id)
            replies.append(
                Reply(
                    text="\n".join(body),
                    buttons=(
                        (
                            Button("Выбрать действие", callback("rec", "choose", code)),
                            Button("Отложить", callback("rec", "snooze", code)),
                        ),
                        (
                            Button("Не подходит", callback("rec", "reject", code)),
                            Button("Почему", callback("rec", "why", code)),
                        ),
                    ),
                )
            )
        return replies

    if not rest:
        return [Reply(text="Кнопка устарела.")]
    from fintracker.application.conversation.callbacks import _resolve_uuid

    recommendation_id = await _resolve_uuid(
        settings,
        workspace_id=workspace_id,
        user_id=actor.user_id,
        table="recommendations",
        prefix=rest[0],
    )
    decision = {
        "choose": "chosen",
        "snooze": "snoozed",
        "reject": "rejected",
        "done": "done",
    }.get(action)
    if decision is None:
        if action == "why":
            async with session_scope(
                settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
            ) as session:
                row = (
                    await session.execute(
                        select(Recommendation).where(Recommendation.id == recommendation_id)
                    )
                ).scalar_one()
                refs = ", ".join(str(item) for item in row.metric_refs) or "нет"
            return [
                Reply(
                    text=(
                        f"Основание: {row.observation}\n"
                        f"Использованные показатели: {refs}\n"
                        f"Версия данных: {row.revision_vector}"
                    )
                )
            ]
        return [Reply(text="Действие недоступно.")]

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        existing = (
            await session.execute(
                select(RecommendationFeedback).where(
                    RecommendationFeedback.workspace_id == workspace_id,
                    RecommendationFeedback.recommendation_id == recommendation_id,
                    RecommendationFeedback.membership_id == actor.membership_id,
                )
            )
        ).scalar_one_or_none()
        snooze_until = dt.date.today() + dt.timedelta(days=7) if decision == "snoozed" else None
        review_date = dt.date.today() + dt.timedelta(days=14) if decision == "chosen" else None
        if existing is None:
            assert actor.membership_id is not None
            session.add(
                RecommendationFeedback(
                    workspace_id=workspace_id,
                    recommendation_id=recommendation_id,
                    membership_id=actor.membership_id,
                    decision=decision,
                    scope="personal",
                    snooze_until=snooze_until,
                    review_date=review_date,
                )
            )
        else:
            existing.decision = decision
            existing.snooze_until = snooze_until
            existing.review_date = review_date
            existing.version += 1

    texts = {
        "chosen": (
            "Намерение сохранено вместе с датой проверки. Лимит, журнал, подписка "
            "и банковские операции при этом не изменены."
        ),
        "snoozed": "Предложение отложено и не вернётся до выбранной даты.",
        "rejected": (
            "Учту. Предупреждения о лимите по этой статье продолжают работать по своим настройкам."
        ),
        "done": (
            "Отметка сохранена. Фактическая экономия не объявляется доказанной: "
            "полнота учёта и разовые события учитываются при оценке."
        ),
    }
    return [Reply(text=texts[decision])]


async def weekly_review_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Недельный обзор по запросу (FR-55)."""
    from fintracker.application.analytics.reviews import build_weekly_review

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        review = await build_weekly_review(session, workspace=workspace, today=today)
    return [
        Reply(
            text=review.render(),
            buttons=(
                (
                    Button("Итог периода", callback("menu", "summary")),
                    Button("Отчёт", callback("menu", "analytics")),
                ),
                (Button("← Меню", callback("menu", "main")),),
            ),
        )
    ]


async def period_summary_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Итог текущего периода с разделением потоков (FR-56)."""
    from fintracker.application.analytics.reviews import build_period_summary

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        summary = await build_period_summary(
            session, workspace=workspace, period_id=period.id, today=today
        )
    return [
        Reply(
            text=summary.render(),
            buttons=(
                (
                    Button("План на следующий", callback("menu", "nextplan")),
                    Button("Обзор недели", callback("menu", "review")),
                ),
                (Button("← Меню", callback("menu", "main")),),
            ),
        )
    ]


async def next_plan_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace
) -> list[Reply]:
    """Проект плана следующего периода с основаниями строк (FR-61)."""
    from fintracker.application.analytics.reviews import build_next_period_draft

    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        current = await period_for_date(session, workspace_id=workspace_id, day=today)
        # Следующий период материализуется тем же вызовом (FR-92).
        following = await period_for_date(
            session, workspace_id=workspace_id, day=current.end_exclusive
        )
        draft = await build_next_period_draft(
            session, workspace=workspace, period_id=following.id, today=today
        )
    return [
        Reply(
            text=draft.render(workspace.currency),
            buttons=(
                (
                    Button("Перенести остатки", callback("menu", "budget")),
                    Button("Изменить лимиты", callback("menu", "categories")),
                ),
                (Button("← Меню", callback("menu", "main")),),
            ),
        )
    ]
