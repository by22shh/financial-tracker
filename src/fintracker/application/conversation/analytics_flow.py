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
from fintracker.application.conversation.views import format_range, money
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
    lines.extend(["", "🔮 Прогноз до конца периода"])
    lines.append(f"Уже потрачено: {money(forecast.fact_minor, workspace.currency)}")
    if forecast.commitments_minor:
        lines.append(
            f"Предстоящие платежи: {money(forecast.commitments_minor, workspace.currency)}"
        )
    if forecast.flexible_forecast_minor is not None:
        lines.append(
            "Остальные траты при нынешнем темпе: "
            f"{money(forecast.flexible_forecast_minor, workspace.currency)}"
        )
    if forecast.total_minor is not None:
        lines.append(f"Итого к концу периода: ≈ {money(forecast.total_minor, workspace.currency)}")
    elif status.completeness == "incomplete":
        lines.append(
            "Прогноз появится, когда вы отметите учёт полным: иначе пропущенные траты "
            "исказят темп. Кнопка — в «Бюджет» → «Проверить учёт»."
        )
    else:
        lines.append("Прогноз появится через неделю наблюдений: пока мало данных о темпе.")
    return [
        Reply(
            text="\n".join(lines),
            buttons=(
                (
                    Button("💡 Рекомендации", callback("rec", "list")),
                    Button("🗂 Категории", callback("menu", "categories")),
                ),
                (
                    Button("📊 Обзор недели", callback("menu", "review")),
                    Button("📋 Итог периода", callback("menu", "summary")),
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

    direct = await _direct_answer(settings, actor=actor, workspace=workspace, question=question)
    if direct is not None:
        return direct

    if "я потратил" in lowered or "я потратила" in lowered:
        # «Сколько я потратил» допускает разные смыслы (FR-58).
        return [
            Reply(
                text=(
                    "✍️ Уточните, что посчитать\n\nТраты, которые вы записали сами, "
                    "которые оплатили вы, или сделанные для вас?"
                ),
                buttons=(
                    (
                        Button("✍️ Записал я", callback("rep", "actor")),
                        Button("💳 Платил я", callback("rep", "spender")),
                    ),
                    (Button("🎁 Для меня", callback("rep", "beneficiary")),),
                ),
            )
        ]

    calendar_month = "календарн" in lowered
    if not calendar_month and "месяц" in lowered:
        # Недельный цикл нельзя называть месяцем: разрез уточняется (A229, FR-60).
        async with session_scope(
            settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
        ) as session:
            current = await period_for_date(session, workspace_id=workspace_id, day=today)
        length_days = (current.end_exclusive - current.start_date).days
        if length_days < 28:
            return [
                Reply(
                    text=(
                        "✍️ Ваш бюджетный период короче месяца: "
                        + format_range(
                            current.start_date, current.end_exclusive - dt.timedelta(days=1)
                        )
                        + ".\n\nПоказать календарный месяц или текущий период?"
                    ),
                    buttons=(
                        (
                            Button("Календарный месяц", callback("rep", "calendar")),
                            Button("Текущий период", callback("rep", "period")),
                        ),
                    ),
                )
            ]

    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        if calendar_month:
            date_from = today.replace(day=1)
            next_month = (date_from + dt.timedelta(days=32)).replace(day=1)
            date_to_exclusive = next_month
            method_note = "за календарный месяц"
        else:
            date_from = period.start_date
            date_to_exclusive = period.end_exclusive
            method_note = "за текущий период бюджета"
        report = await spending_report(
            session,
            workspace=workspace,
            date_from=date_from,
            date_to_exclusive=date_to_exclusive,
            coverage="incomplete",
        )
    return [
        Reply(
            text=format_report(report, title=f"📊 Расходы {method_note}"),
            buttons=(
                (
                    Button("🔎 Детализация", callback("menu", "history")),
                    Button("Календарный месяц", callback("rep", "calendar")),
                ),
            ),
        )
    ]


async def report_slice(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, slice_name: str
) -> list[Reply]:
    """Разрез отчёта, выбранный кнопкой уточнения (FR-58, FR-60, A229)."""
    workspace_id = actor.require_workspace()
    today = dt.datetime.now(ZoneInfo(workspace.timezone)).date()
    from fintracker.application.analytics.reports import FilterSpec

    filters = FilterSpec()
    method_note = "за текущий период бюджета"
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        period = await period_for_date(session, workspace_id=workspace_id, day=today)
        date_from = period.start_date
        date_to_exclusive = period.end_exclusive
        match slice_name:
            case "calendar":
                date_from = today.replace(day=1)
                date_to_exclusive = (date_from + dt.timedelta(days=32)).replace(day=1)
                method_note = "за календарный месяц"
            case "actor":
                filters = FilterSpec(actor_user_ids=(actor.user_id,))
                method_note = "— записи, которые добавили вы"
            case "spender":
                if actor.person_id is None:
                    return [
                        Reply(
                            text=(
                                "ℹ️ Бот пока не знает, какие покупки оплатили вы\n\n"
                                "Укажите своё имя в разделе «Участники» — после этого можно "
                                "будет считать ваши траты."
                            ),
                            buttons=((Button("👥 Участники", callback("menu", "members")),),),
                        )
                    ]
                filters = FilterSpec(spender_person_ids=(actor.person_id,))
                method_note = "— оплаченные вами"
            case "beneficiary":
                if actor.beneficiary_id is None:
                    return [
                        Reply(
                            text=(
                                "ℹ️ Бот пока не знает, какие траты сделаны для вас\n\n"
                                "Укажите своё имя в разделе «Участники»."
                            ),
                            buttons=((Button("👥 Участники", callback("menu", "members")),),),
                        )
                    ]
                filters = FilterSpec(beneficiary_ids=(actor.beneficiary_id,))
                method_note = "— сделанные для вас"
        report = await spending_report(
            session,
            workspace=workspace,
            date_from=date_from,
            date_to_exclusive=date_to_exclusive,
            filters=filters,
            coverage="incomplete",
        )
    return [
        Reply(
            text=format_report(report, title=f"📊 Расходы {method_note}"),
            buttons=(
                (
                    Button("🔎 Детализация", callback("menu", "history")),
                    Button("← Меню", callback("menu", "main")),
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
                        "💡 Рекомендаций пока нет\n\nБот предлагает идеи, когда накопится "
                        "история трат: обычно после первого полного периода."
                    ),
                    buttons=((Button("← Меню", callback("menu", "main")),),),
                )
            ]
        replies: list[Reply] = []
        groups_seen: set[str] = set()
        for row in rows:
            body = ["💡 Идея для вашего бюджета", "", row.observation, ""]
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
                            Button("✅ Выбрать действие", callback("rec", "choose", code)),
                            Button("🕓 Отложить", callback("rec", "snooze", code)),
                        ),
                        (
                            Button("✕ Не подходит", callback("rec", "reject", code)),
                            Button("🔎 Почему", callback("rec", "why", code)),
                        ),
                    ),
                )
            )
        return replies

    if not rest:
        return [Reply(text="🔄 Кнопка устарела.\n\nОткройте нужный раздел заново.")]
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
                refs = str(len(row.metric_refs))
            return [
                Reply(
                    text=(
                        "🔎 Почему появилась рекомендация\n\n"
                        f"{row.observation}"
                        "\n\nУчтено показателей: "
                        f"{refs}"
                        ". Рекомендация основана на записанных данных бюджета; "
                        "пропущенные траты могут повлиять на выводы."
                    )
                )
            ]
        return [Reply(text="🔄 Действие недоступно.")]

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
            "✅ Отметил, что вы решили так сделать. Бот напомнит проверить результат. "
            "Сами лимиты и записи не изменились."
        ),
        "snoozed": "⏰ Отложено — бот вернётся к этой идее позже.",
        "rejected": ("👌 Учту. Предупреждения о лимите этой категории будут приходить как раньше."),
        "done": ("✅ Отметка сохранена. Реальную экономию бот оценит по следующим тратам."),
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
                    Button("📋 Итог периода", callback("menu", "summary")),
                    Button("📊 Отчёт", callback("menu", "analytics")),
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
                    Button("📅 Следующий план", callback("menu", "nextplan")),
                    Button("📊 Обзор недели", callback("menu", "review")),
                ),
                (Button("← Меню", callback("menu", "main")),),
            ),
        )
    ]


async def next_plan_view(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, page: int = 0
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
    pages = max(1, (len(draft.lines) + 7) // 8)
    page = min(max(0, page), pages - 1)
    navigation = []
    if page:
        navigation.append(Button("← Предыдущие категории", callback("nplan", str(page - 1))))
    if page + 1 < pages:
        navigation.append(Button("Следующие категории →", callback("nplan", str(page + 1))))
    return [
        Reply(
            text=draft.render(workspace.currency, page=page),
            buttons=((tuple(navigation),) if navigation else ())
            + (
                (
                    Button("↪️ Перенести остатки", callback("roll", "show")),
                    Button(
                        "✏️ Изменить лимиты",
                        callback("nlimit", "show", short(following.id), "0"),
                    ),
                ),
                (Button("← Меню", callback("menu", "main")),),
            ),
        )
    ]


_REMAINING_WORDS = ("осталось", "остаток", "осталась", "можно потратить", "сколько можно", "хватит")


async def _direct_answer(
    settings: Settings, *, actor: ActorContext, workspace: Workspace, question: str
) -> list[Reply] | None:
    """Прямой ответ по плану периода: остаток и траты категории (FR-58).

    Числа берутся из статуса периода, а не из модели: «сколько осталось» —
    остаток плана, «сколько на продукты» — факт и лимит этой категории.
    """
    from fintracker.application.catalog.categories import list_categories
    from fintracker.application.catalog.keywords import suggest_category
    from fintracker.application.catalog.normalize import normalize_name
    from fintracker.application.conversation.views import plural

    lowered = normalize_name(question)
    asks_remaining = any(word in lowered for word in _REMAINING_WORDS)
    workspace_id = actor.require_workspace()
    async with session_scope(
        settings, RuntimeRole.API, user_id=actor.user_id, workspace_id=workspace_id
    ) as session:
        categories = await list_categories(session, workspace_id=workspace_id)
    category = None
    for item in categories:
        stem = normalize_name(item.name)[:5]
        if len(stem) >= 3 and stem in lowered:
            category = item
            break
    if category is None:
        suggested = suggest_category(question, ((item.id, item.name) for item in categories))
        category = next((item for item in categories if item.id == suggested), None)
    if category is None and not asks_remaining:
        return None
    status = await current_status(settings, actor=actor, workspace=workspace)
    currency = workspace.currency
    period = format_range(status.start_date, status.end_inclusive)
    days_left = max(
        0, (status.end_inclusive - dt.datetime.now(ZoneInfo(workspace.timezone)).date()).days + 1
    )
    buttons = (
        (
            Button("📒 Бюджет", callback("menu", "budget")),
            Button("🧾 История", callback("menu", "history")),
        ),
    )
    if category is not None:
        line = next((item for item in status.lines if item.category_id == category.id), None)
        fact = line.fact_minor if line is not None else 0
        lines = [f"🗂 {category.name} · {period}", "", f"Потрачено: {money(fact, currency)}"]
        if line is not None and line.effective_limit_minor is not None:
            left = line.effective_limit_minor - fact
            lines.append(f"Лимит: {money(line.effective_limit_minor, currency)}")
            lines.append(
                f"Осталось: {money(left, currency)}"
                if left >= 0
                else f"⚠️ Сверх лимита: {money(-left, currency)}"
            )
        else:
            lines.append("Лимит не задан.")
        return [Reply(text="\n".join(lines), buttons=buttons)]
    lines = [f"💰 Остаток · {period}", ""]
    if status.total_limit_minor is None:
        lines.append(f"Потрачено: {money(status.total_fact_minor, currency)}")
        lines.append("План расходов не задан — остаток считать не от чего.")
    else:
        left = status.total_limit_minor - status.total_fact_minor
        lines.append(
            f"Осталось по плану: {money(left, currency)}"
            if left >= 0
            else f"⚠️ Сверх плана: {money(-left, currency)}"
        )
        lines.append(
            f"Потрачено {money(status.total_fact_minor, currency)} из "
            f"{money(status.total_limit_minor, currency)}"
        )
        if left > 0 and days_left:
            lines.append(
                f"До конца периода {days_left} {plural(days_left, 'день', 'дня', 'дней')} — "
                f"примерно {money(left // days_left, currency)} в день."
            )
    return [Reply(text="\n".join(lines), buttons=buttons)]
