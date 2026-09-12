"""Фабрики доменных объектов для проверок на настоящей PostgreSQL."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.catalog.categories import create_category
from fintracker.application.catalog.directory import create_account, create_beneficiary
from fintracker.application.planning.periods import ensure_periods
from fintracker.application.planning.plan import PlanLineSpec, create_budget_version
from fintracker.core.context import ActorContext, MembershipStatus, Role, WorkspaceState
from fintracker.core.ids import new_generation
from fintracker.db.models.access import Membership, User, Workspace
from fintracker.db.models.planning import BudgetPeriod, PeriodPolicyRow
from fintracker.db.uow import UnitOfWork

TZ = "Asia/Novosibirsk"


@dataclass(slots=True)
class Fixture:
    """Готовый бюджет с администратором, периодом и планом."""

    user: User
    workspace: Workspace
    actor: ActorContext
    period: BudgetPeriod
    uow: UnitOfWork
    categories: dict[str, uuid.UUID]
    beneficiaries: dict[str, uuid.UUID]
    accounts: dict[str, uuid.UUID]


async def build_fixture(
    session: AsyncSession,
    *,
    name: str = "Тестовый бюджет",
    currency: str = "RUB",
    start: dt.date = dt.date(2026, 9, 10),
    categories: tuple[str, ...] = ("Продукты", "Рестораны", "Транспорт"),
    beneficiaries: tuple[str, ...] = ("Ниджат", "Софа"),
    accounts: tuple[tuple[str, str], ...] = (
        ("Карта", "full_tracking"),
        ("Кошелёк", "full_tracking"),
    ),
    limits: dict[str, int] | None = None,
    telegram_user_id: int | None = None,
) -> Fixture:
    """Создать бюджет напрямую, минуя диалог, для проверок домена."""
    user = User(
        id=uuid.uuid4(),
        telegram_user_id=telegram_user_id or int(uuid.uuid4().int % 10**9),
    )
    session.add(user)
    await session.flush()
    workspace = Workspace(
        id=uuid.uuid4(),
        name=name,
        currency=currency,
        timezone=TZ,
        state=WorkspaceState.ACTIVE.value,
        admin_user_id=user.id,
    )
    session.add(workspace)
    await session.flush()
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

    actor = ActorContext(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        workspace_id=workspace.id,
        role=Role.ADMIN,
        membership_generation=generation,
        membership_id=membership.id,
        correlation_id="test",
    )
    uow = UnitOfWork(session=session, correlation_id="test")

    session.add(
        PeriodPolicyRow(
            workspace_id=workspace.id,
            version=1,
            anchor_date=start,
            anchor_day=start.day,
            mode="calendar_months",
            interval=1,
            timezone=TZ,
            first_end_exclusive=dt.date(start.year, start.month + 1, start.day)
            if start.month < 12
            else dt.date(start.year + 1, 1, start.day),
            effective_from=start,
            created_by=user.id,
        )
    )
    await session.flush()
    await ensure_periods(session, workspace_id=workspace.id, until_date=start)
    period = (
        await session.execute(
            select(BudgetPeriod)
            .where(BudgetPeriod.workspace_id == workspace.id)
            .order_by(BudgetPeriod.start_date)
            .limit(1)
        )
    ).scalar_one()

    category_ids: dict[str, uuid.UUID] = {}
    for title in categories:
        view = await create_category(session, uow, actor=actor, name=title)
        category_ids[title] = view.id

    beneficiary_ids: dict[str, uuid.UUID] = {}
    for title in beneficiaries:
        view = await create_beneficiary(session, uow, actor=actor, name=title, kind="person")
        beneficiary_ids[title] = view.id
    common = await create_beneficiary(session, uow, actor=actor, name="Общее", kind="common")
    beneficiary_ids["Общее"] = common.id

    account_ids: dict[str, uuid.UUID] = {}
    for title, mode in accounts:
        view = await create_account(
            session,
            uow,
            actor=actor,
            name=title,
            currency=currency,
            mode=mode,
            opening_balance_minor=0 if mode == "full_tracking" else None,
            opening_date=start if mode == "full_tracking" else None,
        )
        account_ids[title] = view.id

    plan_lines = [
        PlanLineSpec(
            category_id=category_ids[title],
            limit_minor=(limits or {}).get(title),
        )
        for title in categories
    ]
    for kind in ("baseline", "working"):
        await create_budget_version(
            session,
            workspace_id=workspace.id,
            period_id=period.id,
            kind=kind,
            plan_status="approved",
            origin="wizard",
            lines=plan_lines,
            approved_by=user.id,
        )
    await session.flush()
    return Fixture(
        user=user,
        workspace=workspace,
        actor=actor,
        period=period,
        uow=uow,
        categories=category_ids,
        beneficiaries=beneficiary_ids,
        accounts=account_ids,
    )
