"""ActorContext — проверенная личность и полномочия команды (ADR-06)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    LEFT = "left"
    REMOVED = "removed"


class WorkspaceState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Контекст действующего лица после проверки членства.

    Создаётся только сервером из доверенного источника личности. Значения из
    текста AI, callback и формы не могут его подменить.
    """

    user_id: uuid.UUID
    telegram_user_id: int
    workspace_id: uuid.UUID | None = None
    role: Role | None = None
    membership_generation: uuid.UUID | None = None
    membership_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    beneficiary_id: uuid.UUID | None = None
    correlation_id: str = ""
    is_system: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    def require_workspace(self) -> uuid.UUID:
        from fintracker.core.errors import PermissionDenied

        if self.workspace_id is None:
            raise PermissionDenied("Не выбран бюджет для этого действия")
        return self.workspace_id


@dataclass(frozen=True, slots=True)
class SystemContext:
    """Контекст системной задачи (открытие периода, очистка, обзор).

    Системная задача имеет только разрешённую команду и конкретный бюджет;
    универсального обхода проверок нет (ADR-06).
    """

    workspace_id: uuid.UUID
    job_kind: str
    correlation_id: str = ""

    def as_actor(self) -> ActorContext:
        return ActorContext(
            user_id=uuid.UUID(int=0),
            telegram_user_id=0,
            workspace_id=self.workspace_id,
            role=None,
            correlation_id=self.correlation_id,
            is_system=True,
        )
