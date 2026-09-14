"""Протокол SecurityChange из пяти шагов (ADR-14, SEC-10, AR-31).

Каждое изменение доступа проходит: fence → prepared в независимом журнале →
атомарное применение в БД под блокировкой бюджета → committed в журнале →
снятие fence. Успех не объявляется раньше окончания протокола; вызов
хранилища никогда не удерживает блокировку БД.
"""

from __future__ import annotations

import datetime as dt
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
    DomainError,
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
    precheck: ApplyFn | None = None,
) -> SecurityChangeResult:
    """Выполнить изменение доступа по протоколу ADR-14 с исходом отказа.

    Отклонение до применения — такой же определённый исход, как успех: fence
    снимается, операция помечается failed, а в журнал пишется доказанный
    отказ, чтобы восстановление не считало её незавершённой (ADR-14, R-11).
    Неопределённый внешний исход fence не снимает.
    """
    journal = security_log or build_security_log(settings.security_log)
    operation = operation_id or uuid.uuid4()
    progress: dict[str, bool] = {"prepared": False, "applied": False}
    try:
        return await _run_fenced_change(
            settings,
            workspace_id=workspace_id,
            kind=kind,
            initiated_by=initiated_by,
            apply=apply,
            correlation_id=correlation_id,
            operation_id=operation,
            allow_states=allow_states,
            security_log=journal,
            acting_user_id=acting_user_id,
            precheck=precheck,
            progress=progress,
        )
    except DomainError as exc:
        if progress["applied"]:
            # Изменение уже применено: снятие fence решается шагами 4–5.
            raise
        await _abort_security_change(
            settings,
            journal=journal,
            operation=operation,
            workspace_id=workspace_id,
            kind=kind,
            acting_user_id=acting_user_id or initiated_by,
            reason=exc.message,
        )
        raise


async def _abort_security_change(
    settings: Settings,
    *,
    journal: SecurityLog,
    operation: uuid.UUID,
    workspace_id: uuid.UUID,
    kind: str,
    acting_user_id: uuid.UUID | None,
    reason: str,
) -> None:
    """Завершить отклонённое изменение доступа и снять его блокировку (R-11).

    Фактическое состояние журнала проверяется: недоступный журнал оставляет
    бюджет заблокированным, потому что исход не доказан.
    """
    try:
        pending = await journal.pending_operations(workspace_id)
    except Exception as exc:
        logger.error(
            "security_abort_journal_unavailable",
            operation_id=str(operation),
            error=type(exc).__name__,
        )
        return
    if str(operation) in pending:
        async with session_scope(
            settings, RuntimeRole.API, user_id=acting_user_id, workspace_id=workspace_id
        ) as session:
            snapshot = await read_access_snapshot(session, workspace_id)
        try:
            await journal.write_aborted(
                operation_id=operation,
                workspace_id=workspace_id,
                kind=kind,
                snapshot=snapshot,
                reason=reason[:300],
                now=_utcnow(),
            )
        except Exception as exc:
            logger.error(
                "security_abort_not_recorded",
                operation_id=str(operation),
                error=type(exc).__name__,
            )
            return

    async with session_scope(
        settings, RuntimeRole.API, user_id=acting_user_id, workspace_id=workspace_id
    ) as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id, Workspace.security_fence == operation)
            .values(security_fence=None, version=Workspace.version + 1)
        )
        await session.execute(
            update(SecurityChange)
            .where(SecurityChange.operation_id == operation)
            .values(state="failed")
        )
    logger.info("security_change_rejected", operation_id=str(operation), kind=kind)


async def _run_fenced_change(
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
    precheck: ApplyFn | None = None,
    progress: dict[str, bool],
) -> SecurityChangeResult:
    """Выполнить изменение доступа по протоколу ADR-14.

    ``precheck`` выполняется до установки fence и отклоняет заведомо неверный
    запрос: опечатка в подтверждении не оставляет бюджет заблокированным
    (AUD-05). ``apply`` вызывается на шаге 3 под блокировкой бюджета и обязана
    быть чисто транзакционной: без сетевых вызовов и ожидания пользователя.
    """
    if kind not in SECURITY_CHANGE_KINDS:
        raise ValueError(f"Неизвестный вид изменения доступа: {kind}")
    journal = security_log or build_security_log(settings.security_log)
    operation = operation_id or uuid.uuid4()
    rls_user = acting_user_id or initiated_by

    if precheck is not None:
        # Проверка заведомо отклоняемых условий до fence; та же проверка
        # повторяется под блокировкой на шаге 3.
        async with session_scope(
            settings,
            RuntimeRole.API,
            user_id=rls_user,
            workspace_id=workspace_id,
            statement_class=StatementClass.COMMAND,
        ) as session:
            probe = UnitOfWork(session=session, correlation_id=correlation_id)
            locked = await probe.lock_workspace(
                workspace_id, allow_states=allow_states, allow_fenced=True
            )
            workspace_row = (
                await session.execute(select(Workspace).where(Workspace.id == workspace_id))
            ).scalar_one()
            await precheck(session, probe, workspace_row)
            assert locked is not None

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

    progress["prepared"] = True
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

    progress["applied"] = True

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


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def resume_or_quarantine(
    settings: Settings, workspace_id: uuid.UUID, *, security_log: SecurityLog | None = None
) -> list[str]:
    """Сверить восстановленный доступ с независимым журналом (AR-31, AR-32, AUD-06).

    Правила восстановления:

    * prepared без доказанного commit — неопределённое состояние: карантин;
    * журнал новее восстановленных строк — применяется последняя доказанная
      версия доступа, бюджет остаётся в карантине до проверки человеком;
    * доказанной последней версии нет — доступ не открывается.
    """
    journal = security_log or build_security_log(settings.security_log)
    pending = await journal.pending_operations(workspace_id)
    last = await journal.last_committed(workspace_id)

    # Recovery uses the same workspace RLS boundary as ordinary worker tasks.
    # Migration credentials must never be needed by a running API/worker.
    async with session_scope(settings, RuntimeRole.WORKER, workspace_id=workspace_id) as session:
        workspace = (
            await session.execute(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
        ).scalar_one_or_none()
        if workspace is None:
            return pending

        quarantine = bool(pending)
        if last is None and workspace.acl_revision > 0:
            # У существующего бюджета нет доказанной версии доступа: журнал
            # недоступен, пуст или подменён. Доступ не открывается (ADR-14, G-04).
            quarantine = True
            logger.error(
                "access_journal_missing",
                workspace_id=str(workspace_id),
                database_revision=workspace.acl_revision,
            )
        if last is not None and workspace.acl_revision < last.proposed_acl_revision:
            # База отстала от журнала: применяется доказанная версия доступа.
            await _apply_proven_access(session, workspace=workspace, record=last)
            quarantine = True
            logger.warning(
                "restored_acl_replayed",
                workspace_id=str(workspace_id),
                database_revision=workspace.acl_revision,
                journal_revision=last.proposed_acl_revision,
            )
        if quarantine:
            workspace.quarantined = True
    return pending


DELETION_STATES = (WorkspaceState.DELETING.value, WorkspaceState.DELETED.value)


async def reconcile_access_on_start(
    settings: Settings, *, security_log: SecurityLog | None = None
) -> dict[str, int]:
    """Сверить доступ всех бюджетов с журналом до открытия доступа (AR-31, R-10).

    Вызывается при старте API и исполнителя: восстановленная из копии база
    сверяется с независимым журналом раньше, чем принимаются запросы и задачи.
    """
    from sqlalchemy import text

    async with session_scope(settings, RuntimeRole.WORKER) as session:
        rows = (await session.execute(text("SELECT id FROM access_recovery_workspaces()"))).all()
    checked = 0
    quarantined = 0
    for (workspace_id,) in rows:
        pending = await resume_or_quarantine(settings, workspace_id, security_log=security_log)
        checked += 1
        if pending:
            quarantined += 1
    report = {"checked": checked, "pending_operations": quarantined}
    logger.info("access_reconciled", **report)
    return report


async def _replay_workspace_state(
    session: AsyncSession, *, workspace: Workspace, state: str
) -> None:
    """Вернуть бюджет в доказанное состояние, включая удаление (R-10).

    Точное время удаления журналом не доказано, поэтому срок очистки
    отсчитывается от момента сверки: восстановление не сокращает окно.
    """
    from fintracker.db.models.access import BudgetDeletionRecord

    if not state:
        return
    workspace.state = state
    if state not in DELETION_STATES:
        return
    now = _utcnow()
    if workspace.deleted_at is None:
        workspace.deleted_at = now
    record = (
        await session.execute(
            select(BudgetDeletionRecord).where(BudgetDeletionRecord.workspace_id == workspace.id)
        )
    ).scalar_one_or_none()
    if record is None:
        session.add(
            BudgetDeletionRecord(
                workspace_id=workspace.id,
                initiated_by=workspace.admin_user_id,
                purge_after=now + dt.timedelta(hours=24),
                state="pending",
                recipients=[],
            )
        )
    await session.flush()
    logger.warning("restored_deletion_replayed", workspace_id=str(workspace.id), state=state)


async def _apply_proven_access(session: AsyncSession, *, workspace: Workspace, record: Any) -> None:
    """Восстановить доказанный снимок доступа целиком (AUD-06, R-10).

    Воспроизводятся членства с их поколениями, версия ACL, администратор и
    состояние бюджета: удаление, подтверждённое журналом, не должно исчезать
    после восстановления из копии.
    """
    snapshot = record.snapshot
    await _replay_workspace_state(session, workspace=workspace, state=str(snapshot.state or ""))
    proven_users = [uuid.UUID(str(item["user_id"])) for item in snapshot.members]
    # Demote first: an administrator transfer must not transiently violate the
    # unique active-administrator index while restoring the new administrator.
    await session.execute(
        update(Membership).where(Membership.workspace_id == workspace.id).values(role="member")
    )
    # A complete snapshot grants no access to members absent from that snapshot.
    await session.execute(
        update(Membership)
        .where(Membership.workspace_id == workspace.id, Membership.user_id.not_in(proven_users))
        .values(status="removed", rejoin_blocked=True, generation=uuid.uuid4())
    )
    existing_users = set(
        (
            await session.execute(
                select(Membership.user_id).where(Membership.workspace_id == workspace.id)
            )
        ).scalars()
    )
    for item in snapshot.members:
        user_id = uuid.UUID(str(item["user_id"]))
        if user_id not in existing_users:
            session.add(
                Membership(
                    workspace_id=workspace.id,
                    user_id=user_id,
                    status=item["status"],
                    role=item["role"],
                    generation=uuid.UUID(str(item["generation"])),
                    rejoin_blocked=str(item.get("rejoin_blocked", "False")) == "True",
                )
            )
            continue
        await session.execute(
            update(Membership)
            .where(
                Membership.workspace_id == workspace.id,
                Membership.user_id == user_id,
            )
            .values(
                status=item["status"],
                role=item["role"],
                generation=uuid.UUID(str(item["generation"])),
                rejoin_blocked=str(item.get("rejoin_blocked", "False")) == "True",
            )
        )
    workspace.acl_revision = max(workspace.acl_revision, record.proposed_acl_revision)
    if snapshot.admin_user_id:
        workspace.admin_user_id = uuid.UUID(str(snapshot.admin_user_id))
    await session.flush()


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
