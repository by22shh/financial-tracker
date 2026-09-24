"""Обзоры, ранний риск и объяснение изменений (FR-41, FR-42, FR-55, FR-56, FR-59).

Числа считает сервис аналитики; AI лишь формулирует объяснение. Перенос
лимита между категориями не называется экономией, а сокращение будущих трат
сопровождается условиями расчёта.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.analytics.reports import (
    ComparisonResult,
    SpendingReport,
    compare_periods,
    spending_report,
)
from fintracker.application.commitments.schedules import materialize_occurrences, upcoming_payments
from fintracker.application.planning.plan import LimitState, LineStatus, period_status
from fintracker.core.money import Money
from fintracker.db.models.access import Workspace
from fintracker.db.models.commitments import Goal
from fintracker.db.models.planning import BudgetPeriod

# Стартовые пороги раннего риска (FORM-11, FR-42). Настраиваются бюджетом.
EARLY_RISK_ABSOLUTE_MINOR = 50_000
EARLY_RISK_RELATIVE = 0.10


@dataclass(frozen=True, slots=True)
class RiskLine:
    """Строка с риском перерасхода (FR-42)."""

    line: LineStatus
    forecast_minor: int
    excess_minor: int
    basis: str

    def describe(self, currency: str) -> str:
        name = self.line.category_name
        if self.line.beneficiary_name:
            name = f"{name} · {self.line.beneficiary_name}"
        return (
            f"{name}: при нынешнем темпе выйдет {Money(self.forecast_minor, currency).format()} "
            f"при лимите {Money(self.line.effective_limit_minor or 0, currency).format()} — "
            f"больше на {Money(self.excess_minor, currency).format()}"
        )


def early_risk_lines(
    *,
    lines: tuple[LineStatus, ...],
    observed_days: int,
    remaining_days: int,
    absolute_threshold_minor: int = EARLY_RISK_ABSOLUTE_MINOR,
    relative_threshold: float = EARLY_RISK_RELATIVE,
) -> list[RiskLine]:
    """Строки, где прогноз превышает лимит сверх порогов (FR-42, FORM-11).

    Прогноз не скрывает текущий факт и строится только при достаточных данных:
    простая скорость трат применяется после семи наблюдаемых дней.
    """
    if observed_days < 7 or remaining_days <= 0:
        return []
    risky: list[RiskLine] = []
    for line in lines:
        if line.limit_state is not LimitState.POSITIVE or not line.effective_limit_minor:
            # Для незаданного и нулевого лимита действует отдельная логика.
            continue
        if line.is_protected:
            continue
        daily = line.fact_minor / observed_days
        forecast = int(line.fact_minor + daily * remaining_days) + line.commitments_minor
        excess = forecast - line.effective_limit_minor
        if excess <= 0:
            continue
        threshold = max(
            absolute_threshold_minor, int(line.effective_limit_minor * relative_threshold)
        )
        if excess < threshold:
            continue
        risky.append(
            RiskLine(
                line=line,
                forecast_minor=forecast,
                excess_minor=excess,
                basis=(f"темп {observed_days} наблюдаемых дней плюс неисполненные обязательства"),
            )
        )
    return sorted(risky, key=lambda item: -item.excess_minor)


@dataclass(frozen=True, slots=True)
class WeeklyReview:
    """Недельный обзор (FR-55)."""

    date_from: dt.date
    date_to_inclusive: dt.date
    spent_minor: int
    comparison: ComparisonResult
    top_changes: tuple[ChangeContribution, ...]
    upcoming: tuple[tuple[str, dt.date, int], ...]
    goals: tuple[tuple[str, int, int | None], ...]
    completeness: str
    risky: tuple[RiskLine, ...]
    currency: str
    suggested_action: str

    def render(self) -> str:
        from fintracker.application.delivery.render import format_date, format_range

        lines = [
            f"📊 Обзор недели · {format_range(self.date_from, self.date_to_inclusive)}",
            "",
            f"Потрачено: {Money(self.spent_minor, self.currency).format()}",
        ]
        delta = self.comparison.absolute_change_minor
        signed = ("+" if delta > 0 else "") + Money(delta, self.currency).format()
        if self.comparison.percent_change is None:
            if self.comparison.previous_minor == 0 and self.spent_minor:
                lines.append("Неделей раньше трат не было.")
            else:
                lines.append(f"По сравнению с прошлой неделей: {signed}")
        else:
            percent = f"{self.comparison.percent_change:+.0f}%".replace(".", ",")
            lines.append(f"По сравнению с прошлой неделей: {signed} ({percent})")
        if self.top_changes:
            lines.append("\n📈 Что изменилось сильнее всего")
            lines.extend(f"• {item.describe(self.currency)}" for item in self.top_changes)
        if self.upcoming:
            lines.append("\n🗓 Ближайшие платежи")
            lines.extend(
                f"• {name} — {format_date(due)}, {Money(amount, self.currency).format()}"
                for name, due, amount in self.upcoming
            )
        if self.goals:
            lines.append("\n🎯 Цели")
            lines.extend(
                f"• {name}: отложено {Money(allocated, self.currency).format()}"
                + (f" из {Money(target, self.currency).format()}" if target else "")
                for name, allocated, target in self.goals
            )
        if self.risky:
            lines.append("\n⚠️ Может не хватить лимита")
            lines.extend(f"• {item.describe(self.currency)}" for item in self.risky[:2])
        if self.completeness == "incomplete":
            lines.append("\nℹ️ Полнота учёта: не подтверждена — часть трат может быть не внесена.")
        # Ровно одно предлагаемое действие (FR-55).
        lines.append(f"\n💡 Что сделать\n{self.suggested_action}")
        return "\n".join(lines)


async def build_weekly_review(
    session: AsyncSession,
    *,
    workspace: Workspace,
    today: dt.date,
    days: int = 7,
) -> WeeklyReview:
    """Обзор недели с сопоставимым интервалом предыдущего периода (FR-55)."""
    date_from = today - dt.timedelta(days=days - 1)
    date_to_exclusive = today + dt.timedelta(days=1)
    current = await spending_report(
        session,
        workspace=workspace,
        date_from=date_from,
        date_to_exclusive=date_to_exclusive,
    )
    previous = await spending_report(
        session,
        workspace=workspace,
        date_from=date_from - dt.timedelta(days=days),
        date_to_exclusive=date_from,
    )
    comparison = compare_periods(
        current_minor=current.total_minor,
        previous_minor=previous.total_minor,
        current_days=days,
        previous_days=days,
        current_partial=False,
        previous_complete=True,
    )
    top = explain_changes(current, previous)

    payments = await upcoming_payments(
        session,
        workspace_id=workspace.id,
        today=today,
        horizon_days=14,
        currency=workspace.currency,
    )
    goals = (
        (
            await session.execute(
                select(Goal)
                .where(Goal.workspace_id == workspace.id, Goal.status == "active")
                .order_by(Goal.priority)
                .limit(3)
            )
        )
        .scalars()
        .all()
    )

    period = (
        await session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == workspace.id,
                BudgetPeriod.start_date <= today,
                BudgetPeriod.end_exclusive > today,
            )
        )
    ).scalar_one_or_none()
    risky: tuple[RiskLine, ...] = ()
    completeness = "incomplete"
    if period is not None:
        status = await period_status(
            session,
            workspace_id=workspace.id,
            period_id=period.id,
            currency=workspace.currency,
            today=today,
        )
        completeness = status.completeness
        observed = (today - period.start_date).days + 1
        remaining = (period.end_exclusive - dt.timedelta(days=1) - today).days
        if completeness != "incomplete":
            risky = tuple(
                early_risk_lines(
                    lines=status.lines, observed_days=observed, remaining_days=remaining
                )
            )

    if risky:
        action = (
            f"Присмотритесь к категории «{risky[0].line.category_name}»: при нынешнем темпе "
            "лимита не хватит."
        )
    elif payments:
        nearest = payments[0]
        action = (
            f"Приготовьте деньги на платёж «{nearest.schedule_name}» к "
            f"{_human_date(nearest.due_date)}."
        )
    elif completeness == "incomplete":
        action = (
            "Проверьте, все ли траты недели внесены, и отметьте учёт полным: "
            "«Бюджет» → «Проверить учёт»."
        )
    else:
        action = "Загляните в план следующего периода."

    return WeeklyReview(
        date_from=date_from,
        date_to_inclusive=today,
        spent_minor=current.total_minor,
        comparison=comparison,
        top_changes=top,
        upcoming=tuple(
            (item.schedule_name, item.due_date, item.remaining_minor) for item in payments[:3]
        ),
        goals=tuple((goal.name, goal.allocated_minor, goal.target_minor) for goal in goals),
        completeness=completeness,
        risky=risky,
        currency=workspace.currency,
        suggested_action=action,
    )


@dataclass(frozen=True, slots=True)
class ChangeContribution:
    """Вклад категории в разницу между интервалами (FR-59).

    Количество покупок и средняя сумма разделяются только при настоящих
    отдельных операциях: по импортным агрегатам разрешён анализ суммы.
    """

    label: str
    delta_minor: int
    current_minor: int
    previous_minor: int
    count_current: int | None
    count_previous: int | None
    average_current_minor: int | None
    average_previous_minor: int | None

    @property
    def count_available(self) -> bool:
        return self.count_current is not None and self.count_previous is not None

    def describe(self, currency: str) -> str:
        sign = "+" if self.delta_minor > 0 else ""
        text = f"{self.label}: {sign}{Money(self.delta_minor, currency).format()}"
        if not self.count_available:
            return text
        assert self.count_current is not None and self.count_previous is not None
        count_delta = self.count_current - self.count_previous
        parts = [f"покупок {self.count_previous} → {self.count_current}"]
        if self.average_previous_minor is not None and self.average_current_minor is not None:
            parts.append(
                "средний чек "
                f"{Money(self.average_previous_minor, currency).format()} → "
                f"{Money(self.average_current_minor, currency).format()}"
            )
        by_count = bool(
            count_delta
            and self.average_previous_minor
            and abs(count_delta * self.average_previous_minor) >= abs(self.delta_minor) / 2
        )
        if by_count:
            driver = "чаще покупали" if count_delta > 0 else "реже покупали"
        else:
            driver = "изменились суммы покупок"
        return f"{text} ({', '.join(parts)} — {driver})"


def explain_changes(
    current: SpendingReport, previous: SpendingReport, limit: int = 3
) -> tuple[ChangeContribution, ...]:
    """Вклад категорий в разницу по записям (FR-59).

    Возвращается только числовой вклад: причинная связь не утверждается.
    """
    before = {row.label: row for row in previous.rows}
    after = {row.label: row for row in current.rows}
    contributions: list[ChangeContribution] = []
    for label in dict.fromkeys(list(after) + list(before)):
        now = after.get(label)
        was = before.get(label)
        delta = (now.amount_minor if now else 0) - (was.amount_minor if was else 0)
        if not delta:
            continue
        individual_only = not (now and now.has_aggregate) and not (was and was.has_aggregate)
        count_now = (
            now.individual_count if now and individual_only else (0 if individual_only else None)
        )
        count_was = (
            was.individual_count if was and individual_only else (0 if individual_only else None)
        )
        contributions.append(
            ChangeContribution(
                label=label,
                delta_minor=delta,
                current_minor=now.amount_minor if now else 0,
                previous_minor=was.amount_minor if was else 0,
                count_current=count_now,
                count_previous=count_was,
                average_current_minor=(
                    int(now.amount_minor / count_now) if now and count_now else None
                ),
                average_previous_minor=(
                    int(was.amount_minor / count_was) if was and count_was else None
                ),
            )
        )
    contributions.sort(key=lambda item: -abs(item.delta_minor))
    return tuple(contributions[:limit])


@dataclass(frozen=True, slots=True)
class PeriodSummary:
    """Итог периода (FR-56)."""

    period_id: uuid.UUID
    date_from: dt.date
    date_to_inclusive: dt.date
    income_minor: int
    consumption_minor: int
    other_flows_minor: int
    goal_contributions_minor: int
    baseline_limit_minor: int | None
    working_limit_minor: int | None
    completeness: str
    unspent_limits_minor: int | None
    open_commitments_minor: int
    currency: str

    def render(self) -> str:
        from fintracker.application.delivery.render import format_range

        lines = [
            f"📋 Итоги периода · {format_range(self.date_from, self.date_to_inclusive)}",
            "",
            f"Доходы: {Money(self.income_minor, self.currency).format()}",
            f"Расходы: {Money(self.consumption_minor, self.currency).format()}",
        ]
        if self.other_flows_minor:
            lines.append(
                "Переводы, займы и другие движения: "
                f"{Money(self.other_flows_minor, self.currency).format()}"
            )
        if self.goal_contributions_minor:
            lines.append(
                f"Отложено на цели: {Money(self.goal_contributions_minor, self.currency).format()}"
            )
        if self.working_limit_minor is not None:
            deviation = self.consumption_minor - self.working_limit_minor
            lines.append("")
            lines.append(
                f"План расходов: {Money(self.working_limit_minor, self.currency).format()}"
            )
            if (
                self.baseline_limit_minor is not None
                and self.baseline_limit_minor != self.working_limit_minor
            ):
                lines.append(
                    "В начале периода план был: "
                    f"{Money(self.baseline_limit_minor, self.currency).format()}"
                )
            if deviation > 0:
                lines.append(f"⚠️ Сверх плана: {Money(deviation, self.currency).format()}")
            else:
                lines.append(
                    f"✅ В рамках плана, запас: {Money(-deviation, self.currency).format()}"
                )
        if self.unspent_limits_minor:
            # Формулировка «вы сэкономили» не применяется ко всем остаткам (FR-56).
            lines.append(
                "Не потрачено по лимитам категорий: "
                f"{Money(self.unspent_limits_minor, self.currency).format()}"
            )
            if self.open_commitments_minor:
                lines.append(
                    "Из них нужно на предстоящие платежи: "
                    f"{Money(self.open_commitments_minor, self.currency).format()}"
                )
        if self.completeness == "incomplete":
            lines.append(
                "\nℹ️ Полнота учёта не подтверждена: часть трат может быть не внесена, "
                "поэтому остаток пока нельзя считать экономией."
            )
        return "\n".join(lines)


async def build_period_summary(
    session: AsyncSession, *, workspace: Workspace, period_id: uuid.UUID, today: dt.date
) -> PeriodSummary:
    """Итог закрытого или текущего периода (FR-56)."""
    from sqlalchemy import func

    from fintracker.application.planning.plan import current_budget_version
    from fintracker.db.models.ledger import Allocation, Transaction, TransactionRevision
    from fintracker.db.models.planning import BudgetLine

    period = (
        await session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == workspace.id, BudgetPeriod.id == period_id
            )
        )
    ).scalar_one()
    status = await period_status(
        session,
        workspace_id=workspace.id,
        period_id=period_id,
        currency=workspace.currency,
        today=today,
    )

    totals = (
        await session.execute(
            select(Allocation.economic_role, func.sum(Allocation.amount_minor))
            .join(
                Transaction,
                (Transaction.workspace_id == Allocation.workspace_id)
                & (Transaction.id == Allocation.transaction_id)
                & (Transaction.current_revision == Allocation.revision),
            )
            .join(
                TransactionRevision,
                (TransactionRevision.workspace_id == Allocation.workspace_id)
                & (TransactionRevision.transaction_id == Allocation.transaction_id)
                & (TransactionRevision.revision == Allocation.revision),
            )
            .where(
                Allocation.workspace_id == workspace.id,
                Transaction.status == "posted",
                TransactionRevision.occurred_date >= period.start_date,
                TransactionRevision.occurred_date < period.end_exclusive,
            )
            .group_by(Allocation.economic_role)
        )
    ).all()
    by_role = {row[0]: int(row[1]) for row in totals}
    income = by_role.get("income", 0)
    other = (
        by_role.get("external_funding", 0)
        + by_role.get("receivable_increase", 0)
        + by_role.get("receivable_decrease", 0)
        + by_role.get("unclassified", 0)
    )

    goal_contributions = 0
    from fintracker.db.models.commitments import GoalMovement

    goal_total = (
        await session.execute(
            select(func.coalesce(func.sum(GoalMovement.change_minor), 0)).where(
                GoalMovement.workspace_id == workspace.id,
                GoalMovement.kind == "allocate",
                GoalMovement.period_id == period_id,
            )
        )
    ).scalar_one()
    goal_contributions = int(goal_total or 0)

    baseline = await current_budget_version(
        session, workspace_id=workspace.id, period_id=period_id, kind="baseline"
    )
    baseline_limit: int | None = None
    if baseline is not None:
        value = (
            await session.execute(
                select(func.coalesce(func.sum(BudgetLine.limit_minor), 0)).where(
                    BudgetLine.workspace_id == workspace.id,
                    BudgetLine.budget_version_id == baseline.id,
                )
            )
        ).scalar_one()
        baseline_limit = int(value or 0)

    unspent = (
        status.total_limit_minor - status.total_fact_minor
        if status.total_limit_minor is not None
        else None
    )
    commitments = sum(line.commitments_minor for line in status.lines)
    return PeriodSummary(
        period_id=period_id,
        date_from=period.start_date,
        date_to_inclusive=period.end_exclusive - dt.timedelta(days=1),
        income_minor=income,
        consumption_minor=status.total_fact_minor,
        other_flows_minor=other,
        goal_contributions_minor=goal_contributions,
        baseline_limit_minor=baseline_limit,
        working_limit_minor=status.total_limit_minor,
        completeness=status.completeness,
        unspent_limits_minor=max(0, unspent) if unspent is not None else None,
        open_commitments_minor=commitments,
        currency=workspace.currency,
    )


@dataclass(frozen=True, slots=True)
class NextPeriodPlanDraft:
    """Проект плана следующего периода (FR-61).

    Имеющийся остаток и ожидаемый доход показаны раздельно: остаток уже есть
    на отслеживаемых счетах, а доход только ожидается.
    """

    period_id: uuid.UUID
    date_from: dt.date
    date_to_inclusive: dt.date
    repeat_basis: str
    lines: tuple[tuple[str, int | None, str], ...]
    expected_income_minor: int | None
    income_dates: tuple[tuple[str, dt.date | None, int], ...]
    available_balance_minor: int
    commitments_minor: int
    goal_contributions_minor: int
    fund_contributions_minor: int
    flexible_available_minor: int | None
    deficit_minor: int | None
    # Остаток показывается, только если есть счета с полным отслеживанием:
    # без них «0 ₽ на счетах» вводил бы в заблуждение.
    has_tracked_accounts: bool = False

    def render(self, currency: str, *, page: int = 0, page_size: int = 8) -> str:
        from fintracker.application.delivery.render import format_date, format_range

        lines = [
            f"📅 План на {format_range(self.date_from, self.date_to_inclusive)}",
            "",
            f"🔁 {self.repeat_basis}",
            "",
        ]
        if self.expected_income_minor is None:
            lines.append("Ожидаемый доход: не указан")
        else:
            lines.append(f"Ожидаемый доход: {Money(self.expected_income_minor, currency).format()}")
        for name, expected_date, amount in self.income_dates[:5]:
            when = format_date(expected_date) if expected_date else "дата не указана"
            lines.append(f"  · {name} — {when}, {Money(amount, currency).format()}")
        if self.has_tracked_accounts:
            lines.append(
                f"На счетах сейчас: {Money(self.available_balance_minor, currency).format()}"
            )
        if self.commitments_minor:
            lines.append(
                "Платежи до конца периода: "
                f"{Money(self.commitments_minor, currency).format()} "
                "(с учётом неоплаченных прошлых)"
            )
        if self.goal_contributions_minor:
            lines.append(
                f"Взносы на цели: {Money(self.goal_contributions_minor, currency).format()}"
            )
        if self.fund_contributions_minor:
            lines.append(
                f"Нерегулярные фонды: {Money(self.fund_contributions_minor, currency).format()}"
            )
        if self.flexible_available_minor is not None:
            lines.append(
                "Остаётся на обычные траты: "
                f"{Money(self.flexible_available_minor, currency).format()}"
            )
        lines.append("\n🗂 Лимиты по категориям")
        pages = max(1, (len(self.lines) + page_size - 1) // page_size)
        page = min(max(0, page), pages - 1)
        if pages > 1:
            lines.append(f"Страница {page + 1} из {pages}")
        for name, limit, basis in self.lines[page * page_size : (page + 1) * page_size]:
            limit_text = Money(limit, currency).format() if limit is not None else "лимит не задан"
            line = f"• {name}: {limit_text}"
            # Одинаковая основа уже указана один раз над списком. Здесь
            # показываются только настоящие исключения для отдельной категории.
            if basis != self.repeat_basis:
                line += f"\n  ↳ {basis}"
            lines.append(line)
        if self.deficit_minor:
            lines.append(
                f"\n⚠️ Лимиты больше дохода на {Money(self.deficit_minor, currency).format()}\n"
                "Уменьшите лимиты или решите, откуда возьмутся деньги."
            )
        return "\n".join(lines)


async def build_next_period_draft(
    session: AsyncSession, *, workspace: Workspace, period_id: uuid.UUID, today: dt.date
) -> NextPeriodPlanDraft:
    """Собрать проект плана с указанием основания каждой строки (FR-61)."""
    from sqlalchemy import func

    from fintracker.application.planning.plan import current_budget_version
    from fintracker.db.models.catalog import Account, Category
    from fintracker.db.models.ledger import AccountEntry
    from fintracker.db.models.planning import BudgetLine, IncomePlan, IncomeSource

    period = (
        await session.execute(
            select(BudgetPeriod).where(
                BudgetPeriod.workspace_id == workspace.id, BudgetPeriod.id == period_id
            )
        )
    ).scalar_one()
    version = await current_budget_version(session, workspace_id=workspace.id, period_id=period_id)
    carried_over = False
    if version is None:
        # Плана на следующий период ещё нет: показываем повторение последнего
        # согласованного плана как проект, а не как утверждённые лимиты (FR-61).
        previous_period = (
            await session.execute(
                select(BudgetPeriod)
                .where(
                    BudgetPeriod.workspace_id == workspace.id,
                    BudgetPeriod.end_exclusive <= period.start_date,
                )
                .order_by(BudgetPeriod.end_exclusive.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if previous_period is not None:
            version = await current_budget_version(
                session, workspace_id=workspace.id, period_id=previous_period.id
            )
            carried_over = version is not None

    rows: list[tuple[str, int | None, str]] = []
    protected_limit = 0
    flexible_limit = 0
    repeat_basis = "План ещё не собран"
    if version is not None:
        lines = (
            await session.execute(
                select(BudgetLine, Category.name)
                .join(
                    Category,
                    (Category.workspace_id == BudgetLine.workspace_id)
                    & (Category.id == BudgetLine.category_id),
                )
                .where(
                    BudgetLine.workspace_id == workspace.id,
                    BudgetLine.budget_version_id == version.id,
                )
                .order_by(Category.sort_order)
            )
        ).all()
        repeat_basis = {
            "template": "Лимиты повторяются каждый период",
            "wizard": "Лимиты заданы при создании бюджета",
            "manual": "Лимиты перенесены из прошлого плана",
            "proposal": "Принятый проект плана",
            "transition": "План переходного периода",
            "rollover": "С переносом остатков прошлого периода",
        }.get(version.origin, "Лимиты из прошлого плана")
        if carried_over:
            repeat_basis = "Лимиты повторены из прошлого периода"
        for line, name in lines:
            rows.append((name, line.limit_minor, repeat_basis))
            if line.is_protected:
                protected_limit += line.limit_minor or 0
            else:
                flexible_limit += line.limit_minor or 0

    income_plan = (
        await session.execute(
            select(IncomePlan).where(
                IncomePlan.workspace_id == workspace.id, IncomePlan.period_id == period_id
            )
        )
    ).scalar_one_or_none()
    expected_income = income_plan.period_amount_minor if income_plan else None
    if income_plan is None:
        # Доход из мастера повторяется, как и лимиты: иначе следующий план
        # показывал бы «доход не задан», хотя его указали при создании.
        from fintracker.application.planning.rollover import active_template

        template = await active_template(
            session, workspace_id=workspace.id, on_date=period.start_date
        )
        rule = dict(template.income_rule) if template is not None else {}
        if rule.get("precision"):
            candidate = rule.get("period_minor") or rule.get("monthly_minor")
            expected_income = int(candidate) if candidate else None
    income_dates: list[tuple[str, dt.date | None, int]] = []
    if income_plan is not None:
        sources = (
            (
                await session.execute(
                    select(IncomeSource)
                    .where(
                        IncomeSource.workspace_id == workspace.id,
                        IncomeSource.income_plan_id == income_plan.id,
                    )
                    .order_by(IncomeSource.expected_date)
                )
            )
            .scalars()
            .all()
        )
        income_dates = [
            (source.name, source.expected_date, source.amount_minor) for source in sources
        ]

    # The reminder worker only builds a short horizon. A next-period preview
    # must include the entire requested period even before that worker runs.
    await materialize_occurrences(
        session,
        workspace_id=workspace.id,
        until_date=period.end_exclusive - dt.timedelta(days=1),
    )
    payments = await upcoming_payments(
        session,
        workspace_id=workspace.id,
        today=period.start_date,
        horizon_days=(period.end_exclusive - period.start_date).days - 1,
        currency=workspace.currency,
    )
    commitments = sum(item.remaining_minor for item in payments)

    contributions = (
        await session.execute(
            select(Goal.kind, func.coalesce(func.sum(Goal.contribution_minor), 0))
            .where(
                Goal.workspace_id == workspace.id,
                Goal.status == "active",
                Goal.contribution_frequency == "per_period",
            )
            .group_by(Goal.kind)
        )
    ).all()
    by_kind = {row[0]: int(row[1] or 0) for row in contributions}
    goal_contributions = by_kind.get("goal", 0)
    fund_contributions = by_kind.get("fund", 0)

    # Остаток учитывается только по счетам с полным отслеживанием (ADR-03):
    # справочные счета не дают доказанной суммы.
    balance = (
        await session.execute(
            select(func.coalesce(func.sum(AccountEntry.signed_minor), 0))
            .join(
                Account,
                (Account.workspace_id == AccountEntry.workspace_id)
                & (Account.id == AccountEntry.account_id),
            )
            .where(
                AccountEntry.workspace_id == workspace.id,
                Account.mode == "full_tracking",
                Account.currency == workspace.currency,
            )
        )
    ).scalar_one()
    available_balance = int(balance or 0)
    tracked_accounts = (
        await session.execute(
            select(func.count())
            .select_from(Account)
            .where(Account.workspace_id == workspace.id, Account.mode == "full_tracking")
        )
    ).scalar_one()

    deficit: int | None = None
    flexible_available: int | None = None
    if expected_income is not None:
        # Долговой платёж входит в потребность полностью, проценты — в расходы.
        need = protected_limit + flexible_limit + goal_contributions + fund_contributions
        difference = need - expected_income
        deficit = difference if difference > 0 else 0
        flexible_available = max(
            0,
            expected_income - protected_limit - goal_contributions - fund_contributions,
        )

    return NextPeriodPlanDraft(
        period_id=period_id,
        date_from=period.start_date,
        date_to_inclusive=period.end_exclusive - dt.timedelta(days=1),
        repeat_basis=repeat_basis,
        lines=tuple(rows),
        expected_income_minor=expected_income,
        income_dates=tuple(income_dates),
        available_balance_minor=available_balance,
        commitments_minor=commitments,
        goal_contributions_minor=goal_contributions,
        fund_contributions_minor=fund_contributions,
        flexible_available_minor=flexible_available,
        deficit_minor=deficit,
        has_tracked_accounts=bool(tracked_accounts),
    )


def _human_date(value: dt.date) -> str:
    from fintracker.application.delivery.render import format_date

    return format_date(value)
