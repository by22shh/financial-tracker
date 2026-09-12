"""Протокол SecurityChange из пяти шагов (ADR-14, SEC-10, AR-31).

Каждое изменение доступа проходит: fence → prepared в независимом журнале →
атомарное применение в БД под блокировкой бюджета → committed в журнале →
снятие fence. Успех не объявляется раньше окончания протокола; вызов
хранилища никогда не удерживает блокировку БД.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.config import Settings
from fintracker.core.context import MembershipStatus, WorkspaceState
from fintracker.core.errors import (
    ConflictError,
    NotFound,
    TemporarilyUnavailable,
    VersionConflict,
)
from fintracker.core.logging import get_logger
from fintracker.db.models.access import Membership, SecurityChange, Workspace
from fintracker.db.session import RuntimeRole, StatementClass, session_scope
from fintracker.db.uow import UnitOfWork
from fintracker.infra.security_log import (
    AccessSnapshot,
    SecurityLog,
    SecurityLogConflict,
    build_security_log,
)

logger = get_logger("identity.security_change")

SECURITY_CHANGE_KINDS = frozenset(
    {
        "workspace_create",
        "member_join",
        "member_rejoin",
        "member_leave",
        "member_remove",
        "allow_rejoin",
        "admin_transfer",
        "workspace_delete",
    }
)


@dataclass(slots=True)
class SecurityChangeResult:
    operation_id: uuid.UUID
    applied_acl_revision: int
    payload: dict[str, Any]


ApplyFn = Callable[[AsyncSession, UnitOfWork, Workspace], Awaitable[dict[str, Any]]]


async def read_access_snapshot(session: AsyncSession, workspace_id: uuid.UUID) -> AccessSnapshot:
    """Минимальная карта доступа: только технические ID и состояния."""
    workspace = (
        await session.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFound("Бюджет недоступен")
    members = (
        await session.execute(
            select(Membership)
            .where(Membership.workspace_id == workspace_id)
            .order_by(Membership.user_id)
        )
    ).scalars()
    return AccessSnapshot(
        workspace_id=str(workspace.id),
        state=workspace.state,
        admin_user_id=str(workspace.admin_user_id) if workspace.admin_user_id else None,
        acl_revision=workspace.acl_revision,
        members=tuple(
            {
                "user_id": str(m.user_id),
                "role": m.role,
                "status": m.status,
                "generation": str(m.generation),
                "rejoin_blocked": str(m.rejoin_blocked),
            }
            for m in members
        ),
    )


async def run_security_change(
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    kind: str,
    initiated_by: uuid.UUID | None,
    apply: ApplyFn,
    correlation_id: str,
    operation_id: uuid.UUID | None = None,
    allow_states: tuple[str, ...] = (WorkspaceState.ACTIVE.value,),
    security_log: SecurityLog | None = None,
    acting_user_id: uuid.UUID | None = None,
) -> SecurityChangeResult:
    """Выполнить изменение доступа по протоколу ADR-14.

    ``apply`` вызывается на шаге 3 под блокировкой бюджета и обязана быть
    чисто транзакционной: без сетевых вызовов и ожидания пользователя.
    """
    if kind not in SECURITY_CHANGE_KINDS:
        raise ValueError(f"Неизвестный вид изменения доступа: {kind}")
    journal = security_log or build_security_log(settings.security_log)
    operation = operation_id or uuid.uuid4()
    rls_user = acting_user_id or initiated_by

    # --- Шаг 1: fence в короткой транзакции -------------------------------
    async with session_scope(
        settings,
        RuntimeRole.API,
        user_id=rls_user,
        workspace_id=workspace_id,
        statement_class=StatementClass.COMMAND,
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=correlation_id)
        workspace = await uow.lock_workspace(
            workspace_id, allow_states=allow_states, allow_fenced=True
        )
        if workspace.security_fence is not None and workspace.security_fence != operation:
            raise TemporarilyUnavailable(
                "Другое изменение доступа ещё не завершено, повторите позже"
            )
        existing = (
            await session.execute(
                select(SecurityChange).where(SecurityChange.operation_id == operation)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.state == "completed":
            # Повтор завершённой операции безопасен.
            return SecurityChangeResult(
                operation_id=operation,
                applied_acl_revision=existing.proposed_acl_revision,
                payload=dict(existing.payload),
            )
        expected_acl = workspace.acl_revision
        proposed_acl = expected_acl + 1
        if existing is None:
            session.add(
                SecurityChange(
                    workspace_id=workspace_id,
                    operation_id=operation,
                    kind=kind,
                    expected_acl_revision=expected_acl,
                    proposed_acl_revision=proposed_acl,
                    state="fencing",
                    digest="",
                    initiated_by=initiated_by,
                )
            )
        await session.execute(
            update(Workspace).where(Workspace.id == workspace_id).values(security_fence=operation)
        )
        snapshot_before = await read_access_snapshot(session, workspace_id)

    # --- Шаг 2: prepared вне транзакции БД --------------------------------
    try:
        prepared = await journal.write_prepared(
            operation_id=operation,
            workspace_id=workspace_id,
            kind=kind,
            expected_acl_revision=expected_acl,
            proposed_acl_revision=proposed_acl,
            snapshot=snapshot_before,
            previous_version_key=None,
            now=_utcnow(),
        )
    except SecurityLogConflict as exc:
        raise ConflictError(f"Конфликт журнала доступа: {exc}") from exc
    except OSError as exc:
        # Журнал недоступен — изменение не объявляется завершённым (ADR-14).
        logger.error("security_log_unavailable", operation_id=str(operation), phase="prepared")
        raise TemporarilyUnavailable(
            "Журнал изменений доступа недоступен, изменение не выполнено"
        ) from exc

    async with session_scope(
        settings, RuntimeRole.API, user_id=rls_user, workspace_id=workspace_id
    ) as session:
        await session.execute(
            update(SecurityChange)
            .where(SecurityChange.operation_id == operation)
            .values(state="prepared", digest=prepared.digest)
        )

    # --- Шаг 3: атомарное применение под блокировкой ----------------------
    async with session_scope(
        settings, RuntimeRole.API, user_id=rls_user, workspace_id=workspace_id
    ) as session:
        uow = UnitOfWork(session=session, correlation_id=correlation_id)
        locked = await uow.lock_workspace(
            workspace_id, allow_states=allow_states, allow_fenced=True
        )
        if locked.security_fence != operation:
            raise ConflictError("Fence изменения доступа не совпадает")
        if locked.acl_revision != expected_acl:
            raise VersionConflict("Права бюджета изменились параллельно")
        row = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        payload = await apply(session, uow, row)
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(acl_revision=proposed_acl, version=Workspace.version + 1)
        )
        await session.execute(
            update(SecurityChange)
            .where(SecurityChange.operation_id == operation)
            .values(state="applied", payload=payload)
        )
        snapshot_after = await read_access_snapshot(session, workspace_id)
        # Снимок читается до commit, поэтому подставляем итоговую версию.
        snapshot_after = AccessSnapshot(
            workspace_id=snapshot_after.workspace_id,
            state=snapshot_after.state,
            admin_user_id=snapshot_after.admin_user_id,
            acl_revision=proposed_acl,
            members=snapshot_after.members,
        )

    # --- Шаг 4: committed вне транзакции БД -------------------------------
    try:
        await journal.write_committed(
            operation_id=operation,
            workspace_id=workspace_id,
            kind=kind,
            applied_acl_revision=proposed_acl,
            snapshot=snapshot_after,
            now=_utcnow(),
        )
    except SecurityLogConflict as exc:
        raise ConflictError(f"Конфликт журнала доступа: {exc}") from exc
    except OSError as exc:
        logger.error("security_log_unavailable", operation_id=str(operation), phase="committed")
        raise TemporarilyUnavailable(
            "Изменение применено, но не подтверждено в журнале доступа; "
            "оператор получил сигнал, доступ остаётся приостановленным"
        ) from exc

    # --- Шаг 5: снятие fence и завершение ---------------------------------
    async with session_scope(
        settings, RuntimeRole.API, user_id=rls_user, workspace_id=workspace_id
    ) as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id, Workspace.security_fence == operation)
            .values(security_fence=None, version=Workspace.version + 1)
        )
        await session.execute(
            update(SecurityChange)
            .where(SecurityChange.operation_id == operation)
            .values(state="completed")
        )
    logger.info(
        "security_change_completed",
        operation_id=str(operation),
        kind=kind,
        acl_revision=proposed_acl,
    )
    return SecurityChangeResult(
        operation_id=operation, applied_acl_revision=proposed_acl, payload=payload
    )


def _utcnow() -> Any:
    import datetime as dt

    return dt.datetime.now(dt.UTC)


async def resume_or_quarantine(
    settings: Settings, workspace_id: uuid.UUID, *, security_log: SecurityLog | None = None
) -> list[str]:
    """prepared без доказанного commit означает карантин бюджета (AR-31, AR-32).

    Используется при восстановлении: доступ не открывается по устаревшему
    списку, неопределённые состояния остаются в карантине.
    """
    journal = security_log or build_security_log(settings.security_log)
    pending = await journal.pending_operations(workspace_id)
    if pending:
        async with session_scope(settings, RuntimeRole.OWNER, workspace_id=workspace_id) as session:
            await session.execute(
                update(Workspace).where(Workspace.id == workspace_id).values(quarantined=True)
            )
    return pending


async def deactivate_membership(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    new_status: MembershipStatus,
    initiated_by: uuid.UUID | None,
    security_change_id: uuid.UUID,
    block_rejoin: bool = False,
) -> Membership:
    """Общая часть выхода и исключения (FR-80, FR-81)."""
    from fintracker.db.models.access import MembershipHistory

    membership = (
        await session.execute(
            select(Membership)
            .where(Membership.workspace_id == workspace_id, Membership.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if membership is None or membership.status != MembershipStatus.ACTIVE.value:
        raise NotFound("Активное членство не найдено")
    previous_status = membership.status
    previous_role = membership.role
    membership.status = new_status.value
    membership.rejoin_blocked = block_rejoin
    membership.access_version += 1
    membership.version += 1
    from sqlalchemy import func

    membership.left_at = func.now()
    session.add(
        MembershipHistory(
            workspace_id=workspace_id,
            user_id=user_id,
            from_status=previous_status,
            to_status=new_status.value,
            from_role=previous_role,
            to_role=previous_role,
            generation=membership.generation,
            initiated_by=initiated_by,
            security_change_id=security_change_id,
        )
    )
    return membership
