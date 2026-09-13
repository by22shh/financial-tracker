"""Unit of Work: блокировка бюджета, версии и исходящие события (ADR-04, ADR-05).

Реализует TECH-04: READ COMMITTED, SELECT FOR UPDATE строки бюджета,
повторная проверка членства и expected_version после блокировки,
фиксированный порядок блокировок зависимых объектов.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from fintracker.core.context import ActorContext, WorkspaceState
from fintracker.core.errors import (
    NotFound,
    PermissionDenied,
    TemporarilyUnavailable,
    VersionConflict,
)
from fintracker.core.fencing import fence_is_valid
from fintracker.db.models.access import Workspace
from fintracker.db.models.platform import OutboxEvent

# Фиксированный порядок блокировок: workspace -> membership/invite -> агрегаты.
LOCK_ORDER = ("workspace", "membership", "invite", "aggregate")


@dataclass(slots=True)
class LockedWorkspace:
    """Результат блокировки строки бюджета."""

    id: uuid.UUID
    name: str
    currency: str
    timezone: str
    state: str
    version: int
    acl_revision: int
    data_revision: int
    plan_revision: int
    calendar_revision: int
    catalog_revision: int
    coverage_revision: int
    event_seq: int
    admin_user_id: uuid.UUID | None
    security_fence: uuid.UUID | None
    quarantined: bool


@dataclass(slots=True)
class UnitOfWork:
    """Координатор одной транзакции команды.

    Внутри транзакции запрещены AI, Telegram, объектное хранилище, ожидание
    пользователя и тяжёлое декодирование (ADR-04).
    """

    session: AsyncSession
    correlation_id: str
    _pending_events: list[dict[str, Any]] = field(default_factory=list)

    async def lock_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        allow_states: tuple[str, ...] = (WorkspaceState.ACTIVE.value,),
        allow_fenced: bool = False,
        actor: ActorContext | None = None,
        require_admin: bool = False,
    ) -> LockedWorkspace:
        """SELECT FOR UPDATE строки бюджета — первый шаг любой команды.

        При переданном ``actor`` под той же блокировкой перепроверяются
        членство, его поколение и роль: результат долгой операции не проходит
        со устаревшим контекстом доступа (ADR-06, AUD-04).
        """
        try:
            row = (
                await self.session.execute(
                    select(Workspace).where(Workspace.id == workspace_id).with_for_update()
                )
            ).scalar_one_or_none()
        except Exception as exc:  # lock_timeout -> временная занятость
            if "lock timeout" in str(exc).lower() or "55P03" in str(exc):
                raise TemporarilyUnavailable(
                    "Запись бюджета сейчас занята, попробуйте ещё раз"
                ) from exc
            raise
        if row is None:
            raise NotFound("Бюджет недоступен")
        if row.state not in allow_states:
            raise PermissionDenied(f"Действие недоступно в состоянии бюджета «{row.state}»")
        if row.security_fence is not None and not allow_fenced:
            raise TemporarilyUnavailable(
                "Идёт изменение доступа к бюджету, повторите через несколько секунд"
            )
        if row.quarantined and not allow_fenced:
            raise TemporarilyUnavailable("Бюджет находится в карантине после восстановления")
        if actor is not None:
            await self.check_actor(actor, require_admin=require_admin)
        if not await fence_is_valid(self.session):
            # Аренда фоновой задачи потеряна во время выполнения команды:
            # результат не фиксируется этим исполнителем (ADR-05, R-02).
            raise TemporarilyUnavailable("Право на выполнение задачи утрачено, запись не сохранена")
        return LockedWorkspace(
            id=row.id,
            name=row.name,
            currency=row.currency,
            timezone=row.timezone,
            state=row.state,
            version=row.version,
            acl_revision=row.acl_revision,
            data_revision=row.data_revision,
            plan_revision=row.plan_revision,
            calendar_revision=row.calendar_revision,
            catalog_revision=row.catalog_revision,
            coverage_revision=row.coverage_revision,
            event_seq=row.event_seq,
            admin_user_id=row.admin_user_id,
            security_fence=row.security_fence,
            quarantined=row.quarantined,
        )

    async def check_actor(self, actor: ActorContext, *, require_admin: bool = False) -> None:
        """Перепроверить актуальные членство, поколение и роль (ADR-06, AUD-04).

        Отзыв доступа во время выполнения команды отменяет её результат: доступ
        подтверждается заново под блокировкой бюджета, а не только при входе.
        """
        from fintracker.core.context import MembershipStatus, Role
        from fintracker.db.models.access import Membership

        workspace_id = actor.require_workspace()
        row = (
            await self.session.execute(
                select(
                    Membership.status,
                    Membership.generation,
                    Membership.role,
                ).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == actor.user_id,
                )
            )
        ).one_or_none()
        if row is None or row[0] != MembershipStatus.ACTIVE.value:
            raise PermissionDenied("Доступ к бюджету прекращён")
        if actor.membership_generation is not None and row[1] != actor.membership_generation:
            raise PermissionDenied("Доступ выдан заново: повторите действие")
        if require_admin and row[2] != Role.ADMIN.value:
            raise PermissionDenied("Действие доступно только администратору бюджета")

    @staticmethod
    def check_expected_version(actual: int, expected: int | None, *, label: str) -> None:
        """Проверка expected_version без автоматического перетирания (ADR-04)."""
        if expected is not None and actual != expected:
            raise VersionConflict(
                f"{label} изменился: ожидалась версия {expected}, сейчас {actual}",
                details={"expected": expected, "actual": actual},
            )

    async def bump_revisions(
        self,
        workspace_id: uuid.UUID,
        *,
        data: bool = False,
        plan: bool = False,
        calendar: bool = False,
        catalog: bool = False,
        coverage: bool = False,
        acl: bool = False,
    ) -> None:
        """Инкремент счётчиков областей — основа актуальности снимков (ADR-09)."""
        values: dict[str, Any] = {"version": Workspace.version + 1}
        if data:
            values["data_revision"] = Workspace.data_revision + 1
        if plan:
            values["plan_revision"] = Workspace.plan_revision + 1
        if calendar:
            values["calendar_revision"] = Workspace.calendar_revision + 1
        if catalog:
            values["catalog_revision"] = Workspace.catalog_revision + 1
        if coverage:
            values["coverage_revision"] = Workspace.coverage_revision + 1
        if acl:
            values["acl_revision"] = Workspace.acl_revision + 1
        await self.session.execute(
            update(Workspace).where(Workspace.id == workspace_id).values(**values)
        )

    async def emit(
        self,
        *,
        workspace_id: uuid.UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: uuid.UUID | None = None,
        aggregate_revision: int | None = None,
        payload: dict[str, Any] | None = None,
        audience: str = "members",
        actor_user_id: uuid.UUID | None = None,
    ) -> OutboxEvent:
        """Сохранить доменное событие в той же транзакции (TECH-05).

        Payload содержит только ID, тип и версию: содержимое перечитывается
        под проверенным контекстом при рендеринге доставки (ADR-06).
        """
        seq = (
            await self.session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(event_seq=Workspace.event_seq + 1)
                .returning(Workspace.event_seq)
            )
        ).scalar_one()
        event = OutboxEvent(
            workspace_id=workspace_id,
            event_seq=seq,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_revision=aggregate_revision,
            event_type=event_type,
            payload=payload or {},
            audience=audience,
            actor_user_id=actor_user_id,
            correlation_id=self.correlation_id,
        )
        self.session.add(event)
        return event

    async def now(self) -> dt.datetime:
        """Время базы — единый источник для сроков внутри транзакции."""
        value = (await self.session.execute(text("SELECT now()"))).scalar_one()
        assert isinstance(value, dt.datetime)
        return value
