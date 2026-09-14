"""Мастер создания бюджета и его однократная публикация (FR-84, FR-85, CMD-02).

Настройка хранится как личный черновик с возможностью вернуться назад.
Категории, обязательства и лимиты публикуются согласованно одним действием;
повтор не создаёт копию бюджета.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.categories import create_category
from fintracker.application.catalog.directory import (
    create_account,
    create_beneficiary,
    create_person,
)
from fintracker.application.identity.security_change import run_security_change
from fintracker.application.planning.periods import ensure_periods
from fintracker.application.planning.plan import PlanLineSpec, create_budget_version
from fintracker.config import Settings
from fintracker.core.calendar import (
    CalendarError,
    DateRange,
    PeriodPolicy,
    RepeatMode,
    validate_timezone,
)
from fintracker.core.context import ActorContext, MembershipStatus, Role, WorkspaceState
from fintracker.core.errors import ConflictError, NotFound, ValidationFailed
from fintracker.core.ids import new_generation
from fintracker.core.money import Money, normalize_currency
from fintracker.db.models.access import (
    BudgetSetupDraft,
    Membership,
    MembershipHistory,
    User,
    Workspace,
)
from fintracker.db.models.planning import (
    BudgetPeriod,
    IncomePlan,
    IncomeSource,
    PeriodPolicyRow,
    RecurringPlanTemplate,
)
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

SCHEMA_VERSION = 1


class WizardStep(StrEnum):
    NAME = "name"
    CURRENCY = "currency"
    TIMEZONE = "timezone"
    PERIOD_DATES = "period_dates"
    PERIOD_REPEAT = "period_repeat"
    INCOME = "income"
    CATEGORIES = "categories"
    LIMITS = "limits"
    COMMITMENTS = "commitments"
    GOALS = "goals"
    TEMPLATE = "template"
    REVIEW = "review"
    DONE = "done"


STEP_ORDER: tuple[WizardStep, ...] = (
    WizardStep.NAME,
    WizardStep.CURRENCY,
    WizardStep.TIMEZONE,
    WizardStep.PERIOD_DATES,
    WizardStep.PERIOD_REPEAT,
    WizardStep.INCOME,
    WizardStep.CATEGORIES,
    WizardStep.LIMITS,
    WizardStep.COMMITMENTS,
    WizardStep.GOALS,
    WizardStep.TEMPLATE,
    WizardStep.REVIEW,
)


@dataclass(slots=True)
class DraftCategory:
    name: str
    limit_minor: int | None = None
    beneficiary_name: str | None = None
    parent_name: str | None = None
    rollover_mode: str = "none"
    is_protected: bool = False


@dataclass(slots=True)
class WizardState:
    """Состояние мастера; хранится в БД, не в памяти процесса (ADR-08)."""

    name: str | None = None
    currency: str | None = None
    timezone: str | None = None
    start_date: dt.date | None = None
    end_inclusive: dt.date | None = None
    repeat_mode: RepeatMode | None = None
    repeat_interval: int | None = None
    income_precision: str | None = None
    income_monthly_minor: int | None = None
    income_period_minor: int | None = None
    income_min_minor: int | None = None
    income_max_minor: int | None = None
    income_sources: list[dict[str, Any]] = field(default_factory=list)
    categories: list[DraftCategory] = field(default_factory=list)
    beneficiaries: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    accounts: list[dict[str, Any]] = field(default_factory=list)
    repeat_template: bool = True
    deficit_accepted: bool = False
    deficit_reason: str | None = None
    overall_limit_minor: int | None = None
    # Введённые в мастере обязательства и цели сохраняются вместе с бюджетом:
    # набранный участником текст не теряется (FR-84, G-14).
    commitments: list[dict[str, Any]] = field(default_factory=list)
    goals: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "currency": self.currency,
            "timezone": self.timezone,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_inclusive": self.end_inclusive.isoformat() if self.end_inclusive else None,
            "repeat_mode": self.repeat_mode.value if self.repeat_mode else None,
            "repeat_interval": self.repeat_interval,
            "income_precision": self.income_precision,
            "income_monthly_minor": self.income_monthly_minor,
            "income_period_minor": self.income_period_minor,
            "income_min_minor": self.income_min_minor,
            "income_max_minor": self.income_max_minor,
            "income_sources": self.income_sources,
            "categories": [
                {
                    "name": category.name,
                    "limit_minor": category.limit_minor,
                    "beneficiary_name": category.beneficiary_name,
                    "parent_name": category.parent_name,
                    "rollover_mode": category.rollover_mode,
                    "is_protected": category.is_protected,
                }
                for category in self.categories
            ],
            "beneficiaries": self.beneficiaries,
            "people": self.people,
            "accounts": self.accounts,
            "repeat_template": self.repeat_template,
            "deficit_accepted": self.deficit_accepted,
            "deficit_reason": self.deficit_reason,
            "overall_limit_minor": self.overall_limit_minor,
            "commitments": self.commitments,
            "goals": self.goals,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WizardState:
        def as_date(value: Any) -> dt.date | None:
            return dt.date.fromisoformat(value) if isinstance(value, str) else None

        return cls(
            name=payload.get("name"),
            currency=payload.get("currency"),
            timezone=payload.get("timezone"),
            start_date=as_date(payload.get("start_date")),
            end_inclusive=as_date(payload.get("end_inclusive")),
            repeat_mode=RepeatMode(payload["repeat_mode"]) if payload.get("repeat_mode") else None,
            repeat_interval=payload.get("repeat_interval"),
            income_precision=payload.get("income_precision"),
            income_monthly_minor=payload.get("income_monthly_minor"),
            income_period_minor=payload.get("income_period_minor"),
            income_min_minor=payload.get("income_min_minor"),
            income_max_minor=payload.get("income_max_minor"),
            income_sources=list(payload.get("income_sources") or []),
            categories=[
                DraftCategory(
                    name=item["name"],
                    limit_minor=item.get("limit_minor"),
                    beneficiary_name=item.get("beneficiary_name"),
                    parent_name=item.get("parent_name"),
                    rollover_mode=item.get("rollover_mode", "none"),
                    is_protected=bool(item.get("is_protected", False)),
                )
                for item in (payload.get("categories") or [])
            ],
            beneficiaries=list(payload.get("beneficiaries") or []),
            people=list(payload.get("people") or []),
            accounts=list(payload.get("accounts") or []),
            repeat_template=bool(payload.get("repeat_template", True)),
            deficit_accepted=bool(payload.get("deficit_accepted", False)),
            deficit_reason=payload.get("deficit_reason"),
            overall_limit_minor=payload.get("overall_limit_minor"),
            commitments=list(payload.get("commitments") or []),
            goals=list(payload.get("goals") or []),
        )

    def policy(self) -> PeriodPolicy:
        if (
            self.start_date is None
            or self.timezone is None
            or self.repeat_mode is None
            or self.repeat_interval is None
        ):
            raise ValidationFailed("Даты и правило повторения ещё не заданы")
        return PeriodPolicy(
            anchor_date=self.start_date,
            mode=self.repeat_mode,
            interval=self.repeat_interval,
            timezone=self.timezone,
        )

    def validate_for_publish(self) -> None:
        if not self.name:
            raise ValidationFailed("Не задано название бюджета")
        if not self.currency:
            raise ValidationFailed("Не выбрана валюта бюджета")
        if not self.timezone:
            raise ValidationFailed("Не выбран часовой пояс")
        if self.start_date is None or self.end_inclusive is None:
            raise ValidationFailed("Не выбраны даты первого периода")
        if self.end_inclusive < self.start_date:
            raise ValidationFailed("Дата конца раньше даты начала")
        policy = self.policy()
        if not policy.matches_first_end(self.end_inclusive):
            raise ValidationFailed(
                "Выбранные даты и правило повторения несогласованы: "
                "уточните конец первого периода или правило"
            )


@dataclass(frozen=True, slots=True)
class FundingCheck:
    """Проверка финансирования плана (FR-62, A148)."""

    limits_total_minor: int
    income_minor: int | None
    deficit_minor: int | None
    income_known: bool


def check_funding(state: WizardState) -> FundingCheck:
    """Дефицит показывается явно; бот не выдумывает доход (FR-62)."""
    limits_total = sum(c.limit_minor or 0 for c in state.categories)
    income = state.income_period_minor
    # Месячная сумма относится к месячному периоду; для других длительностей
    # основание требуется отдельно (FR-85, A217).
    monthly_period = state.repeat_mode is RepeatMode.CALENDAR_MONTHS and state.repeat_interval == 1
    if income is None and state.income_monthly_minor is not None and monthly_period:
        income = state.income_monthly_minor
    if income is None:
        return FundingCheck(
            limits_total_minor=limits_total,
            income_minor=None,
            deficit_minor=None,
            income_known=False,
        )
    deficit = limits_total - income
    return FundingCheck(
        limits_total_minor=limits_total,
        income_minor=income,
        deficit_minor=deficit if deficit > 0 else 0,
        income_known=True,
    )


async def get_or_create_draft(
    session: AsyncSession, *, owner_user_id: uuid.UUID
) -> tuple[BudgetSetupDraft, WizardState]:
    row = (
        await session.execute(
            select(BudgetSetupDraft)
            .where(
                BudgetSetupDraft.owner_user_id == owner_user_id,
                BudgetSetupDraft.state == "draft",
            )
            .order_by(BudgetSetupDraft.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        row = BudgetSetupDraft(
            owner_user_id=owner_user_id,
            state="draft",
            step=WizardStep.NAME.value,
            schema_version=SCHEMA_VERSION,
            payload={},
        )
        session.add(row)
        await session.flush()
    return row, WizardState.from_payload(dict(row.payload))


async def save_draft(
    session: AsyncSession,
    draft: BudgetSetupDraft,
    state: WizardState,
    *,
    step: WizardStep,
    expected_version: int | None = None,
) -> None:
    if expected_version is not None and draft.version != expected_version:
        raise ConflictError("Настройка изменилась в другой сессии")
    draft.payload = state.to_payload()
    draft.step = step.value
    draft.version += 1
    await session.flush()


async def publish_workspace(
    settings: Settings,
    *,
    user: User,
    draft_id: uuid.UUID,
    correlation_id: str,
) -> uuid.UUID:
    """Опубликовать настроенный бюджет одним согласованным действием (FR-84, A143).

    Создание проходит протокол SecurityChange для пока невидимого Workspace
    в состоянии draft (ADR-14).
    """
    async with session_scope(settings, RuntimeRole.API, user_id=user.id) as session:
        draft = (
            await session.execute(
                select(BudgetSetupDraft)
                .where(
                    BudgetSetupDraft.id == draft_id,
                    BudgetSetupDraft.owner_user_id == user.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if draft is None:
            raise NotFound("Настройка недоступна")
        if draft.published_workspace_id is not None:
            # Повтор подтверждения не создаёт второй бюджет (A143).
            return draft.published_workspace_id
        if draft.state == "publishing":
            raise ConflictError("Публикация уже выполняется")
        state = WizardState.from_payload(dict(draft.payload))
        state.validate_for_publish()
        assert state.currency is not None and state.timezone is not None
        currency = normalize_currency(state.currency)
        validate_timezone(state.timezone)
        draft.state = "publishing"
        draft.version += 1
        await session.flush()
        workspace_id = uuid.uuid4()

    # Бюджет создаётся сразу невидимым в состоянии draft под собственным
    # контекстом RLS: сервер сам выбирает его идентификатор (ADR-14).
    async with session_scope(
        settings, RuntimeRole.API, user_id=user.id, workspace_id=workspace_id
    ) as session:
        session.add(
            Workspace(
                id=workspace_id,
                name=state.name or "Бюджет",
                currency=currency,
                timezone=state.timezone,
                state=WorkspaceState.DRAFT.value,
                admin_user_id=None,
            )
        )

    async def apply(session: AsyncSession, uow: UnitOfWork, workspace: Workspace) -> dict[str, str]:
        await _materialize_workspace(
            session, uow, settings=settings, workspace=workspace, user=user, state=state
        )
        return {"workspace_id": str(workspace.id)}

    await run_security_change(
        settings,
        workspace_id=workspace_id,
        kind="workspace_create",
        initiated_by=user.id,
        acting_user_id=user.id,
        apply=apply,
        correlation_id=correlation_id,
        allow_states=(WorkspaceState.DRAFT.value,),
    )

    async with session_scope(
        settings, RuntimeRole.API, user_id=user.id, workspace_id=workspace_id
    ) as session:
        published = (
            await session.execute(
                select(BudgetSetupDraft).where(BudgetSetupDraft.id == draft_id).with_for_update()
            )
        ).scalar_one()
        published.state = "published"
        published.published_workspace_id = workspace_id
        published.step = WizardStep.DONE.value
        published.version += 1
        from fintracker.db.models.access import UserBudgetContext

        context = (
            await session.execute(
                select(UserBudgetContext).where(UserBudgetContext.user_id == user.id)
            )
        ).scalar_one_or_none()
        if context is None:
            session.add(UserBudgetContext(user_id=user.id, workspace_id=workspace_id, version=1))
        else:
            context.workspace_id = workspace_id
            context.version += 1
    return workspace_id


async def _materialize_workspace(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    settings: Settings,
    workspace: Workspace,
    user: User,
    state: WizardState,
) -> None:
    """Создать всё содержимое бюджета в одной транзакции."""
    from fintracker.application.intelligence.schedule import ensure_analysis_preference

    workspace.state = WorkspaceState.ACTIVE.value
    workspace.admin_user_id = user.id
    # Расписание анализа сохраняется вместе с бюджетом (FR-73, R-06).
    await ensure_analysis_preference(session, workspace_id=workspace.id)
    generation = new_generation()
    membership = Membership(
        workspace_id=workspace.id,
        user_id=user.id,
        role=Role.ADMIN.value,
        status=MembershipStatus.ACTIVE.value,
        generation=generation,
    )
    session.add(membership)
    await session.flush()
    session.add(
        MembershipHistory(
            workspace_id=workspace.id,
            user_id=user.id,
            from_status=None,
            to_status=MembershipStatus.ACTIVE.value,
            from_role=None,
            to_role=Role.ADMIN.value,
            generation=generation,
            initiated_by=user.id,
        )
    )

    actor = ActorContext(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        workspace_id=workspace.id,
        role=Role.ADMIN,
        membership_generation=generation,
        membership_id=membership.id,
        correlation_id=uow.correlation_id,
    )

    # --- Календарь ---------------------------------------------------------
    policy = state.policy()
    assert state.start_date is not None and state.end_inclusive is not None
    policy_row = PeriodPolicyRow(
        workspace_id=workspace.id,
        version=1,
        anchor_date=state.start_date,
        anchor_day=state.start_date.day,
        mode=policy.mode.value,
        interval=policy.interval,
        timezone=policy.timezone,
        first_end_exclusive=state.end_inclusive + dt.timedelta(days=1),
        effective_from=state.start_date,
        base_sequence=0,
        created_by=user.id,
    )
    session.add(policy_row)
    await session.flush()

    # --- Справочники -------------------------------------------------------
    beneficiary_ids: dict[str, uuid.UUID] = {}
    for name in state.beneficiaries:
        beneficiary_view = await create_beneficiary(
            session, uow, actor=actor, name=name, kind="person"
        )
        beneficiary_ids[name] = beneficiary_view.id
    common = await create_beneficiary(session, uow, actor=actor, name="Общее", kind="common")
    beneficiary_ids["Общее"] = common.id

    for person_name in state.people:
        await create_person(session, uow, actor=actor, name=person_name)

    for account in state.accounts:
        await create_account(
            session,
            uow,
            actor=actor,
            name=str(account["name"]),
            currency=workspace.currency,
            mode=str(account.get("mode", "reference")),
            account_type=str(account.get("type", "card")),
            opening_balance_minor=account.get("opening_balance_minor"),
            opening_date=(
                dt.date.fromisoformat(str(account["opening_date"]))
                if account.get("opening_date")
                else None
            ),
        )

    category_ids: dict[str, uuid.UUID] = {}
    for item in state.categories:
        parent_id = category_ids.get(item.parent_name or "")
        category_view = await create_category(
            session, uow, actor=actor, name=item.name, parent_id=parent_id
        )
        category_ids[item.name] = category_view.id

    # --- Периоды и план ----------------------------------------------------
    today = dt.datetime.now(dt.UTC).date()
    await ensure_periods(
        session,
        workspace_id=workspace.id,
        until_date=max(today, state.start_date),
    )
    first_period = (
        await session.execute(
            select(BudgetPeriod)
            .where(BudgetPeriod.workspace_id == workspace.id)
            .order_by(BudgetPeriod.start_date)
            .limit(1)
        )
    ).scalar_one()

    plan_lines = [
        PlanLineSpec(
            category_id=category_ids[item.name],
            beneficiary_id=(
                beneficiary_ids.get(item.beneficiary_name) if item.beneficiary_name else None
            ),
            limit_minor=item.limit_minor,
            rollover_mode=item.rollover_mode,
            is_protected=item.is_protected,
        )
        for item in state.categories
    ]
    funding = check_funding(state)
    plan_status = "approved"
    if funding.deficit_minor and not state.deficit_accepted:
        # Дефицит не утверждается молча (FR-62, A148).
        plan_status = "needs_review"

    for kind in ("baseline", "working"):
        await create_budget_version(
            session,
            workspace_id=workspace.id,
            period_id=first_period.id,
            kind=kind,
            plan_status=plan_status,
            origin="wizard",
            lines=plan_lines,
            overall_limit_minor=state.overall_limit_minor,
            approved_by=user.id,
            reason="Создано мастером настройки",
        )

    # --- Повторяемый шаблон плана -----------------------------------------
    if state.repeat_template:
        session.add(
            RecurringPlanTemplate(
                workspace_id=workspace.id,
                version=1,
                enabled=True,
                effective_from=state.start_date,
                approval_actor_id=user.id,
                lines=[
                    {
                        "category_id": str(spec.category_id),
                        "beneficiary_id": str(spec.beneficiary_id) if spec.beneficiary_id else None,
                        "limit_minor": spec.limit_minor,
                        "rollover_mode": spec.rollover_mode,
                        "is_protected": spec.is_protected,
                    }
                    for spec in plan_lines
                ],
                income_rule={
                    "precision": state.income_precision,
                    "monthly_minor": state.income_monthly_minor,
                    "period_minor": state.income_period_minor,
                },
                overall_limit_minor=state.overall_limit_minor,
            )
        )

    # --- План дохода -------------------------------------------------------
    if state.income_precision:
        income_plan = IncomePlan(
            workspace_id=workspace.id,
            period_id=first_period.id,
            precision=state.income_precision,
            basis="monthly_total" if state.income_monthly_minor else "period_total",
            monthly_amount_minor=state.income_monthly_minor,
            period_amount_minor=funding.income_minor,
            min_minor=state.income_min_minor,
            expected_minor=state.income_monthly_minor or state.income_period_minor,
            max_minor=state.income_max_minor,
            detailed_by_sources=bool(state.income_sources),
            unknown_reason=None if funding.income_known else "Основание дохода периода не задано",
            created_by=user.id,
        )
        session.add(income_plan)
        await session.flush()
        for source in state.income_sources:
            session.add(
                IncomeSource(
                    workspace_id=workspace.id,
                    income_plan_id=income_plan.id,
                    name=str(source["name"]),
                    amount_minor=int(source["amount_minor"]),
                    expected_date=(
                        dt.date.fromisoformat(str(source["expected_date"]))
                        if source.get("expected_date")
                        else None
                    ),
                )
            )

    # --- Обязательства и цели, введённые в мастере (FR-84, G-14) -----------
    await _create_planned(session, uow, workspace=workspace, actor=actor, state=state)

    await uow.bump_revisions(workspace.id, calendar=True, plan=True, catalog=True, data=True)
    await uow.emit(
        workspace_id=workspace.id,
        event_type="BudgetCreated",
        aggregate_type="workspace",
        aggregate_id=workspace.id,
        payload={"workspace_id": str(workspace.id), "name": workspace.name},
        audience="author",
        actor_user_id=user.id,
    )


async def _create_planned(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    workspace: Workspace,
    actor: ActorContext,
    state: WizardState,
) -> None:
    """Создать обязательства и цели, названные участником в мастере (G-14)."""
    from decimal import Decimal

    from fintracker.application.commitments.goals import create_goal
    from fintracker.application.commitments.schedules import create_schedule
    from fintracker.domain.parsing.dates import resolve_date_expression
    from fintracker.domain.schedule import ScheduleKind, ScheduleRule

    currency = workspace.currency
    today = state.start_date or dt.date.today()
    for entry in state.commitments:
        amount = Money.from_decimal(Decimal(str(entry["amount_decimal"])), currency)
        anchor_date = today
        raw_due = entry.get("due")
        if raw_due:
            parsed = resolve_date_expression(str(raw_due), reference=today)
            if parsed is not None:
                anchor_date = parsed.value
        await create_schedule(
            session,
            uow,
            actor=actor,
            name=str(entry["name"]),
            direction="payment",
            rule=ScheduleRule(kind=ScheduleKind.MONTHLY, anchor_date=anchor_date),
            currency=currency,
            expected=amount,
        )
    for entry in state.goals:
        await create_goal(
            session,
            uow,
            actor=actor,
            name=str(entry["name"]),
            currency=currency,
            target=Money.from_decimal(Decimal(str(entry["amount_decimal"])), currency),
        )


def preview_periods(state: WizardState, count: int = 3) -> list[DateRange]:
    """Предпросмотр следующих интервалов (FR-90)."""
    try:
        policy = state.policy()
    except (ValidationFailed, CalendarError):
        return []
    return policy.preview(count + 1)[1:]


def money_from_text(value: str, currency: str) -> Money:
    """Разобрать введённую сумму в минимальные единицы."""
    from fintracker.domain.parsing.amounts import parse_amounts

    amounts = parse_amounts(value)
    if not amounts:
        raise ValidationFailed("Не удалось разобрать сумму")
    if amounts[0].is_ambiguous:
        raise ValidationFailed(
            "Сумма допускает несколько прочтений: уточните, например 1500 или 1,5"
        )
    return Money.from_decimal(Decimal(amounts[0].value), currency)
