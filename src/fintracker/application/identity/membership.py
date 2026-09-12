"""Выход, исключение, передача роли и удаление бюджета (FR-80–FR-83).

Все операции проходят протокол SecurityChange: успех не объявляется раньше
окончания записи в независимый журнал доступа.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.application.identity.security_change import (
    deactivate_membership,
    run_security_change,
)
from fintracker.config import Settings
from fintracker.core.context import MembershipStatus, Role, WorkspaceState
from fintracker.core.errors import (
    ConflictError,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from fintracker.core.logging import get_logger
from fintracker.db.models.access import (
    AdminTransferProposal,
    BudgetDeletionRecord,
    BudgetInvite,
    Membership,
    MembershipHistory,
    Workspace,
)
from fintracker.db.models.platform import Candidate, Draft, NotificationDelivery
from fintracker.db.session import RuntimeRole, session_scope
from fintracker.db.uow import UnitOfWork

logger = get_logger("identity.membership")

# Срок предложения передачи администрирования.
ADMIN_TRANSFER_TTL = dt.timedelta(days=7)


@dataclass(frozen=True, slots=True)
class MemberView:
    user_id: uuid.UUID
    telegram_user_id: int
    role: Role
    status: MembershipStatus
    person_name: str | None
    joined_at: dt.datetime


async def list_members(session: AsyncSession, *, workspace_id: uuid.UUID) -> list[MemberView]:
    from fintracker.db.models.access import Person, User

    rows = (
        await session.execute(
            select(Membership, User.telegram_user_id, Person.name)
            .join(User, User.id == Membership.user_id)
            .outerjoin(
                Person,
                (Person.workspace_id == Membership.workspace_id)
                & (Person.id == Membership.person_id),
            )
            .where(
                Membership.workspace_id == workspace_id,
                Membership.status == MembershipStatus.ACTIVE.value,
            )
            .order_by(Membership.role, Membership.joined_at)
        )
    ).all()
    return [
        MemberView(
            user_id=row[0].user_id,
            telegram_user_id=row[1],
            role=Role(row[0].role),
            status=MembershipStatus(row[0].status),
            person_name=row[2],
            joined_at=row[0].joined_at,
        )
        for row in rows
    ]


async def _cancel_personal_material(
    session: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Отменить личные черновики и недоставленные сообщения ушедшего (FR-80).

    Общие расписания, цели и категории остаются; выполняющийся AI не имеет
    права провести результат после отзыва доступа.
    """
    draft_ids = (
        (
            await session.execute(
                select(Draft.id).where(
                    Draft.workspace_id == workspace_id,
                    Draft.owner_user_id == user_id,
                    Draft.state.in_(("received", "processing", "needs_clarification", "ready")),
                )
            )
        )
        .scalars()
        .all()
    )
    if draft_ids:
        await session.execute(
            update(Candidate)
            .where(
                Candidate.workspace_id == workspace_id,
                Candidate.draft_id.in_(draft_ids),
                Candidate.state != "posted",
            )
            .values(state="cancelled")
        )
        await session.execute(
            update(Draft)
            .where(Draft.id.in_(draft_ids))
            .values(state="cancelled", failure_reason="Доступ к бюджету прекращён")
        )
    await session.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.workspace_id == workspace_id,
            NotificationDelivery.recipient_user_id == user_id,
            NotificationDelivery.state.in_(("pending", "failed")),
            NotificationDelivery.delivery_class != "terminal",
        )
        .values(state="cancelled", last_error="Доступ к бюджету прекращён")
    )


async def leave_workspace(
    settings: Settings, *, workspace_id: uuid.UUID, user_id: uuid.UUID, correlation_id: str
) -> None:
    """Выйти из бюджета (FR-80, A168).

    Администратор сначала передаёт роль либо удаляет бюджет (FR-82, A172).
    """

    async def apply(session: AsyncSession, uow: UnitOfWork, workspace: Workspace) -> dict[str, str]:
        membership = (
            await session.execute(
                select(Membership)
                .where(Membership.workspace_id == workspace.id, Membership.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if membership is None or membership.status != MembershipStatus.ACTIVE.value:
            raise NotFound("Активное членство не найдено")
        if membership.role == Role.ADMIN.value:
            raise ConflictError(
                "Сначала передайте администрирование другому участнику или удалите бюджет"
            )
        change_id = uuid.uuid4()
        await deactivate_membership(
            session,
            workspace_id=workspace.id,
            user_id=user_id,
            new_status=MembershipStatus.LEFT,
            initiated_by=user_id,
            security_change_id=change_id,
        )
        await _cancel_personal_material(session, workspace_id=workspace.id, user_id=user_id)
        await uow.emit(
            workspace_id=workspace.id,
            event_type="MemberLeft",
            aggregate_type="membership",
            aggregate_id=membership.id,
            payload={"user_id": str(user_id)},
            actor_user_id=user_id,
        )
        return {"user_id": str(user_id)}

    await run_security_change(
        settings,
        workspace_id=workspace_id,
        kind="member_leave",
        initiated_by=user_id,
        acting_user_id=user_id,
        apply=apply,
        correlation_id=correlation_id,
    )
    # Личный выбор активного бюджета сбрасывается после выхода.
    async with session_scope(settings, RuntimeRole.API, user_id=user_id) as session:
        from fintracker.db.models.access import UserBudgetContext

        await session.execute(
            update(UserBudgetContext)
            .where(
                UserBudgetContext.user_id == user_id,
                UserBudgetContext.workspace_id == workspace_id,
            )
            .values(workspace_id=None, version=UserBudgetContext.version + 1)
        )


async def remove_member(
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    admin_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    correlation_id: str,
) -> None:
    """Исключить участника; статус removed запрещает вход по общему коду (FR-81)."""
    if admin_user_id == target_user_id:
        raise ValidationFailed("Администратор не может исключить себя этим действием")

    async def apply(session: AsyncSession, uow: UnitOfWork, workspace: Workspace) -> dict[str, str]:
        admin = (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace.id,
                    Membership.user_id == admin_user_id,
                    Membership.status == MembershipStatus.ACTIVE.value,
                    Membership.role == Role.ADMIN.value,
                )
            )
        ).scalar_one_or_none()
        if admin is None:
            raise PermissionDenied("Исключать участников может только администратор")
        change_id = uuid.uuid4()
        membership = await deactivate_membership(
            session,
            workspace_id=workspace.id,
            user_id=target_user_id,
            new_status=MembershipStatus.REMOVED,
            initiated_by=admin_user_id,
            security_change_id=change_id,
            block_rejoin=True,
        )
        await _cancel_personal_material(session, workspace_id=workspace.id, user_id=target_user_id)
        await uow.emit(
            workspace_id=workspace.id,
            event_type="MemberRemoved",
            aggregate_type="membership",
            aggregate_id=membership.id,
            payload={"user_id": str(target_user_id)},
            actor_user_id=admin_user_id,
        )
        return {"user_id": str(target_user_id)}

    await run_security_change(
        settings,
        workspace_id=workspace_id,
        kind="member_remove",
        initiated_by=admin_user_id,
        acting_user_id=admin_user_id,
        apply=apply,
        correlation_id=correlation_id,
    )


async def allow_rejoin(
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    admin_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    correlation_id: str,
) -> None:
    """Разрешить исключённому вернуться; членство не создаётся (CMD-06)."""

    async def apply(session: AsyncSession, uow: UnitOfWork, workspace: Workspace) -> dict[str, str]:
        admin = (
            await session.execute(
                select(Membership).where(
                    Membership.workspace_id == workspace.id,
                    Membership.user_id == admin_user_id,
                    Membership.status == MembershipStatus.ACTIVE.value,
                    Membership.role == Role.ADMIN.value,
                )
            )
        ).scalar_one_or_none()
        if admin is None:
            raise PermissionDenied("Разрешать возвращение может только администратор")
        result = await session.execute(
            update(Membership)
            .where(
                Membership.workspace_id == workspace.id,
                Membership.user_id == target_user_id,
                Membership.rejoin_blocked.is_(True),
            )
            .values(rejoin_blocked=False, version=Membership.version + 1)
            .returning(Membership.id)
        )
        if result.scalar_one_or_none() is None:
            raise NotFound("Запрета на вход для этого человека нет")
        return {"user_id": str(target_user_id)}

    await run_security_change(
        settings,
        workspace_id=workspace_id,
        kind="allow_rejoin",
        initiated_by=admin_user_id,
        acting_user_id=admin_user_id,
        apply=apply,
        correlation_id=correlation_id,
    )


async def propose_admin_transfer(
    session: AsyncSession,
    uow: UnitOfWork,
    *,
    workspace_id: uuid.UUID,
    from_user_id: uuid.UUID,
    to_user_id: uuid.UUID,
) -> AdminTransferProposal:
    """Предложить передачу администрирования (FR-82).

    До принятия текущий администратор сохраняет роль.
    """
    workspace = await uow.lock_workspace(workspace_id)
    if workspace.admin_user_id != from_user_id:
        raise PermissionDenied("Передавать администрирование может только администратор")
    target = (
        await session.execute(
            select(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == to_user_id,
                Membership.status == MembershipStatus.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFound("Адресат не является активным участником бюджета")
    existing = (
        await session.execute(
            select(AdminTransferProposal).where(
                AdminTransferProposal.workspace_id == workspace_id,
                AdminTransferProposal.state == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("Предложение передачи уже отправлено")
    row = AdminTransferProposal(
        workspace_id=workspace_id,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        expected_acl_revision=workspace.acl_revision,
        expires_at=dt.datetime.now(dt.UTC) + ADMIN_TRANSFER_TTL,
        state="pending",
    )
    session.add(row)
    await session.flush()
    return row


async def accept_admin_transfer(
    settings: Settings,
    *,
    proposal_id: uuid.UUID,
    acting_user_id: uuid.UUID,
    correlation_id: str,
) -> uuid.UUID:
    """Принять администрирование: роли меняются атомарно (FR-82, A173).

    Бюджет не остаётся без администратора и не получает двух из-за гонки.
    """
    async with session_scope(settings, RuntimeRole.API, user_id=acting_user_id) as session:
        proposal = (
            await session.execute(
                select(AdminTransferProposal).where(AdminTransferProposal.id == proposal_id)
            )
        ).scalar_one_or_none()
        if proposal is None or proposal.to_user_id != acting_user_id:
            raise NotFound("Предложение недоступно")
        workspace_id = proposal.workspace_id

    async def apply(session: AsyncSession, uow: UnitOfWork, workspace: Workspace) -> dict[str, str]:
        row = (
            await session.execute(
                select(AdminTransferProposal)
                .where(AdminTransferProposal.id == proposal_id)
                .with_for_update()
            )
        ).scalar_one()
        now = await uow.now()
        if row.state != "pending":
            raise ConflictError("Предложение уже обработано")
        if row.expires_at <= now:
            row.state = "expired"
            raise ConflictError("Срок предложения истёк")
        if row.expected_acl_revision != workspace.acl_revision:
            # Устаревшее предложение не применяется к новой версии ACL (AR-10).
            row.state = "cancelled"
            raise ConflictError("Состав участников изменился, предложение устарело")

        previous_admin = (
            await session.execute(
                select(Membership)
                .where(
                    Membership.workspace_id == workspace.id,
                    Membership.user_id == row.from_user_id,
                    Membership.status == MembershipStatus.ACTIVE.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        new_admin = (
            await session.execute(
                select(Membership)
                .where(
                    Membership.workspace_id == workspace.id,
                    Membership.user_id == row.to_user_id,
                    Membership.status == MembershipStatus.ACTIVE.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if previous_admin is None or new_admin is None:
            row.state = "cancelled"
            raise ConflictError("Один из участников больше не активен")

        # Сначала снять прежнюю роль, затем назначить новую: deferred проверка
        # видит итог без пустого администратора (DATA_CONTRACT §2.1).
        previous_admin.role = Role.MEMBER.value
        previous_admin.version += 1
        await session.flush()
        new_admin.role = Role.ADMIN.value
        new_admin.version += 1
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(admin_user_id=row.to_user_id)
        )
        row.state = "accepted"
        row.resolved_at = func.now()
        for user_id, to_role in ((row.from_user_id, Role.MEMBER), (row.to_user_id, Role.ADMIN)):
            membership = previous_admin if user_id == row.from_user_id else new_admin
            session.add(
                MembershipHistory(
                    workspace_id=workspace.id,
                    user_id=user_id,
                    from_status=MembershipStatus.ACTIVE.value,
                    to_status=MembershipStatus.ACTIVE.value,
                    from_role=Role.ADMIN.value
                    if user_id == row.from_user_id
                    else Role.MEMBER.value,
                    to_role=to_role.value,
                    generation=membership.generation,
                    initiated_by=acting_user_id,
                )
            )
        await uow.emit(
            workspace_id=workspace.id,
            event_type="AdminTransferred",
            aggregate_type="workspace",
            aggregate_id=workspace.id,
            payload={"user_id": str(row.to_user_id), "from_user_id": str(row.from_user_id)},
            actor_user_id=acting_user_id,
        )
        return {"new_admin": str(row.to_user_id)}

    await run_security_change(
        settings,
        workspace_id=workspace_id,
        kind="admin_transfer",
        initiated_by=acting_user_id,
        acting_user_id=acting_user_id,
        apply=apply,
        correlation_id=correlation_id,
    )
    return workspace_id


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    workspace_id: uuid.UUID
    name: str
    member_count: int
    transaction_count: int
    category_count: int
    goal_count: int
    attachment_count: int


async def deletion_preview(
    session: AsyncSession, *, workspace_id: uuid.UUID, admin_user_id: uuid.UUID
) -> DeletionPreview:
    """Показать состав удаляемых данных без изменений (FR-83, CMD-08)."""
    from fintracker.db.models.catalog import Category
    from fintracker.db.models.commitments import Goal
    from fintracker.db.models.ledger import Transaction
    from fintracker.db.models.platform import Attachment

    workspace = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFound("Бюджет недоступен")
    if workspace.admin_user_id != admin_user_id:
        raise PermissionDenied("Удалить бюджет может только администратор")

    async def count(model: type, *conditions: object) -> int:
        statement = select(func.count()).select_from(model).where(*conditions)  # type: ignore[arg-type]
        return int((await session.execute(statement)).scalar_one())

    return DeletionPreview(
        workspace_id=workspace_id,
        name=workspace.name,
        member_count=await count(
            Membership,
            Membership.workspace_id == workspace_id,
            Membership.status == MembershipStatus.ACTIVE.value,
        ),
        transaction_count=await count(Transaction, Transaction.workspace_id == workspace_id),
        category_count=await count(Category, Category.workspace_id == workspace_id),
        goal_count=await count(Goal, Goal.workspace_id == workspace_id),
        attachment_count=await count(Attachment, Attachment.workspace_id == workspace_id),
    )


async def delete_workspace(
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    admin_user_id: uuid.UUID,
    confirmation_name: str,
    correlation_id: str,
) -> None:
    """Удалить весь бюджет (FR-83, A176, A177).

    Атомарный переход в ``deleting`` блокирует ввод, вступление, экспорт и
    очереди; коды отзываются; участникам уходит только служебное извещение.
    """

    async def apply(session: AsyncSession, uow: UnitOfWork, workspace: Workspace) -> dict[str, str]:
        if workspace.admin_user_id != admin_user_id:
            raise PermissionDenied("Удалить бюджет может только администратор")
        if confirmation_name.strip().casefold() != workspace.name.strip().casefold():
            raise ValidationFailed("Для подтверждения введите точное название бюджета")
        recipients = (
            (
                await session.execute(
                    select(Membership.user_id).where(
                        Membership.workspace_id == workspace.id,
                        Membership.status == MembershipStatus.ACTIVE.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        workspace.state = WorkspaceState.DELETING.value
        workspace.deleted_at = func.now()
        await session.execute(
            update(BudgetInvite)
            .where(
                BudgetInvite.workspace_id == workspace.id,
                BudgetInvite.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )
        await session.execute(
            update(AdminTransferProposal)
            .where(
                AdminTransferProposal.workspace_id == workspace.id,
                AdminTransferProposal.state == "pending",
            )
            .values(state="cancelled")
        )
        # Обычная очередь уведомлений гасится (FR-83).
        await session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.workspace_id == workspace.id,
                NotificationDelivery.state.in_(("pending", "failed")),
            )
            .values(state="cancelled", last_error="Бюджет удалён")
        )
        await session.execute(
            update(Draft)
            .where(
                Draft.workspace_id == workspace.id,
                Draft.state.in_(("received", "processing", "needs_clarification", "ready")),
            )
            .values(state="cancelled", failure_reason="Бюджет удалён")
        )
        session.add(
            BudgetDeletionRecord(
                workspace_id=workspace.id,
                initiated_by=admin_user_id,
                purge_after=dt.datetime.now(dt.UTC) + dt.timedelta(hours=24),
                state="pending",
                recipients=[str(user_id) for user_id in recipients],
            )
        )
        await uow.emit(
            workspace_id=workspace.id,
            event_type="BudgetDeletionRequested",
            aggregate_type="workspace",
            aggregate_id=workspace.id,
            payload={"workspace_name": workspace.name},
            audience="terminal",
            actor_user_id=admin_user_id,
        )
        return {"workspace_id": str(workspace.id)}

    await run_security_change(
        settings,
        workspace_id=workspace_id,
        kind="workspace_delete",
        initiated_by=admin_user_id,
        acting_user_id=admin_user_id,
        apply=apply,
        correlation_id=correlation_id,
        allow_states=(WorkspaceState.ACTIVE.value,),
    )
    logger.info("workspace_deleted", workspace_id=str(workspace_id))
