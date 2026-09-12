"""Разрешение личности и полномочий (FR-01, FR-79, ADR-06)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.context import (
    ActorContext,
    MembershipStatus,
    Role,
    WorkspaceState,
)
from fintracker.core.errors import NotFound, PermissionDenied
from fintracker.db.models.access import Membership, User, UserBudgetContext, Workspace


@dataclass(frozen=True, slots=True)
class BudgetListItem:
    workspace_id: uuid.UUID
    name: str
    role: Role
    short_id: str
    is_active_context: bool
    currency: str
    state: str


async def ensure_user(session: AsyncSession, *, telegram_user_id: int, locale: str = "ru") -> User:
    """Создать или найти пользователя по проверенному Telegram ID (FR-01).

    Username, имя и телефон не являются основанием доступа.
    """
    statement = (
        pg_insert(User)
        .values(id=uuid.uuid4(), telegram_user_id=telegram_user_id, locale=locale)
        .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
        .returning(User.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is not None:
        user = (await session.execute(select(User).where(User.id == inserted))).scalar_one()
        return user
    return (
        await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    ).scalar_one()


async def get_active_workspace_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """Личный выбор активного бюджета (FR-79)."""
    return (
        await session.execute(
            select(UserBudgetContext.workspace_id).where(UserBudgetContext.user_id == user_id)
        )
    ).scalar_one_or_none()


async def list_budgets(session: AsyncSession, user_id: uuid.UUID) -> list[BudgetListItem]:
    """«Мои бюджеты»: только собственные активные членства (FR-79, CMD-02)."""
    from fintracker.core.ids import short_id

    active_id = await get_active_workspace_id(session, user_id)
    rows = (
        await session.execute(
            select(Membership, Workspace)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(
                Membership.user_id == user_id,
                Membership.status == MembershipStatus.ACTIVE.value,
                Workspace.state.in_((WorkspaceState.ACTIVE.value, WorkspaceState.DRAFT.value)),
            )
            .order_by(Workspace.name)
        )
    ).all()
    return [
        BudgetListItem(
            workspace_id=workspace.id,
            name=workspace.name,
            role=Role(membership.role),
            short_id=short_id(workspace.id),
            is_active_context=workspace.id == active_id,
            currency=workspace.currency,
            state=workspace.state,
        )
        for membership, workspace in rows
    ]


async def resolve_actor(
    session: AsyncSession,
    *,
    user: User,
    workspace_id: uuid.UUID,
    correlation_id: str = "",
    require_admin: bool = False,
    allow_states: tuple[str, ...] = (WorkspaceState.ACTIVE.value,),
) -> ActorContext:
    """Проверить активное членство и собрать контекст действия (ADR-06).

    Проверяется каждая команда: состояние бюджета, статус членства, поколение,
    роль. Неизвестный или недоступный бюджет даёт NotFound без раскрытия данных.
    """
    row = (
        await session.execute(
            select(Membership, Workspace)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(Membership.workspace_id == workspace_id, Membership.user_id == user.id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Бюджет недоступен")
    membership, workspace = row
    if workspace.state not in allow_states:
        raise NotFound("Бюджет недоступен")
    if membership.status != MembershipStatus.ACTIVE.value:
        raise NotFound("Бюджет недоступен")
    if require_admin and membership.role != Role.ADMIN.value:
        raise PermissionDenied("Действие доступно только администратору бюджета")
    return ActorContext(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        workspace_id=workspace.id,
        role=Role(membership.role),
        membership_generation=membership.generation,
        membership_id=membership.id,
        person_id=membership.person_id,
        beneficiary_id=membership.beneficiary_id,
        correlation_id=correlation_id,
    )


async def set_active_workspace(
    session: AsyncSession,
    *,
    user: User,
    workspace_id: uuid.UUID,
    expected_version: int | None = None,
) -> int:
    """Выбрать активный бюджет только для себя (CMD-03, FR-79)."""
    await resolve_actor(session, user=user, workspace_id=workspace_id)
    current = (
        await session.execute(select(UserBudgetContext).where(UserBudgetContext.user_id == user.id))
    ).scalar_one_or_none()
    if current is None:
        row = UserBudgetContext(user_id=user.id, workspace_id=workspace_id, version=1)
        session.add(row)
        return 1
    if expected_version is not None and current.version != expected_version:
        from fintracker.core.errors import VersionConflict

        raise VersionConflict("Выбор бюджета изменился в другой сессии")
    current.workspace_id = workspace_id
    current.version += 1
    return current.version
