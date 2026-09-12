"""Пользователи, бюджеты, членства и доступ (DATA_CONTRACT §2.1)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fintracker.db.base import Base, now_server, pk_uuid, version_column


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = pk_uuid()
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ru'"))
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'active'"))
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("status IN ('active','deleting','deleted')", name="status_allowed"),
    )


class Workspace(Base):
    """Бюджет как постоянное пространство совместного учёта (FR-02)."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = pk_uuid()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ru'"))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'draft'"))
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    version: Mapped[int] = version_column()
    # Fence протокола SecurityChange: ненулевой => финансовые команды приостановлены
    security_fence: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    acl_revision: Mapped[int] = version_column()
    data_revision: Mapped[int] = version_column()
    plan_revision: Mapped[int] = version_column()
    calendar_revision: Mapped[int] = version_column()
    catalog_revision: Mapped[int] = version_column()
    coverage_revision: Mapped[int] = version_column()
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('draft','active','deleting','deleted')", name="state_allowed"),
        CheckConstraint("char_length(currency) = 3", name="currency_len"),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_not_blank"),
        UniqueConstraint("id", "currency"),
    )


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Случайный UUID, не счётчик (ADR-14): откат не может повторить поколение.
    generation: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    access_version: Mapped[int] = version_column()
    rejoin_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    beneficiary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assume_self_spender: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    autopost_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    large_amount_threshold_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    joined_at: Mapped[dt.datetime] = now_server()
    left_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = version_column()

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id"),
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("role IN ('admin','member')", name="role_allowed"),
        CheckConstraint("status IN ('active','left','removed')", name="status_allowed"),
        Index("ix_memberships_user_status", "user_id", "status", "workspace_id"),
        Index(
            "uq_memberships_single_admin",
            "workspace_id",
            unique=True,
            postgresql_where=text("role = 'admin' AND status = 'active'"),
        ),
        Index("ix_memberships_ws_status", "workspace_id", "status"),
    )


class MembershipHistory(Base):
    """Неизменяемые переходы членства (DATA_CONTRACT §2.1)."""

    __tablename__ = "membership_history"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    from_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    generation: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    security_change_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    occurred_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (Index("ix_membership_history_ws", "workspace_id", "occurred_at"),)


class UserBudgetContext(Base):
    """Личный выбор активного бюджета (FR-79)."""

    __tablename__ = "user_budget_contexts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version: Mapped[int] = version_column()
    updated_at: Mapped[dt.datetime] = now_server()


class BudgetSetupDraft(Base):
    """Возобновляемый мастер создания бюджета (FR-84), личный до публикации."""

    __tablename__ = "budget_setup_drafts"

    id: Mapped[uuid.UUID] = pk_uuid()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'draft'"))
    step: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'name'"))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    published_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), unique=True, nullable=True
    )
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "state IN ('draft','publishing','published','cancelled')", name="state_allowed"
        ),
        Index("ix_budget_setup_drafts_owner", "owner_user_id", "state"),
    )


class BudgetInvite(Base):
    """Отзываемый код приглашения (FR-77). Секрет хранится только как digest."""

    __tablename__ = "budget_invites"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    secret_digest: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    digest_key_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'member'"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False)
    used_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("max_uses > 0", name="max_uses_positive"),
        CheckConstraint("used_uses >= 0", name="used_uses_non_negative"),
        CheckConstraint("used_uses <= max_uses", name="used_within_quota"),
        CheckConstraint("role = 'member'", name="role_member_only"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_budget_invites_ws", "workspace_id", "revoked_at"),
    )


class InviteAttempt(Base):
    """Ограничение попыток ввода кода (FR-78, LIM-02)."""

    __tablename__ = "invite_attempts"

    id: Mapped[uuid.UUID] = pk_uuid()
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempted_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (Index("ix_invite_attempts_tg", "telegram_user_id", "attempted_at"),)


class AdminTransferProposal(Base):
    __tablename__ = "admin_transfer_proposals"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    from_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    to_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expected_acl_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','accepted','declined','expired','cancelled')",
            name="state_allowed",
        ),
        CheckConstraint("from_user_id <> to_user_id", name="distinct_users"),
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_admin_transfer_pending",
            "workspace_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
    )


class SecurityChange(Base):
    """Протокол изменения доступа (ADR-14, SEC-10)."""

    __tablename__ = "security_changes"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    expected_acl_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    proposed_acl_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'fencing'"))
    digest: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "state IN ('fencing','prepared','applied','committed','completed','failed')",
            name="state_allowed",
        ),
        CheckConstraint(
            "kind IN ('workspace_create','member_join','member_rejoin','member_leave',"
            "'member_remove','allow_rejoin','admin_transfer','workspace_delete')",
            name="kind_allowed",
        ),
        Index("ix_security_changes_ws_state", "workspace_id", "state"),
    )


class Person(Base):
    """Человек, совершивший покупку (FR-88). Профиль не даёт доступа."""

    __tablename__ = "people"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    aliases: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_people_active_name",
            "workspace_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )


class Beneficiary(Base):
    """Получатель расхода: человек или «Общее» (FR-03)."""

    __tablename__ = "beneficiaries"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    person_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("kind IN ('person','common')", name="kind_allowed"),
        CheckConstraint(
            "(kind = 'person') OR (kind = 'common' AND person_id IS NULL)",
            name="common_has_no_person",
        ),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "person_id"],
            ["people.workspace_id", "people.id"],
            name="fk_beneficiaries_person_same_workspace",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_beneficiaries_active_name",
            "workspace_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )


class BudgetDeletionRecord(Base):
    """Журнал удаления бюджета, применяемый и после восстановления (FR-83)."""

    __tablename__ = "budget_deletion_records"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False)
    initiated_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requested_at: Mapped[dt.datetime] = now_server()
    purge_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'pending'"))
    recipients: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    purged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('pending','purging','purged')", name="state_allowed"),
    )


class AuditEvent(Base):
    """Проверяемый след финансовых и административных изменений."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    system_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    object_type: Mapped[str] = mapped_column(String(60), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (Index("ix_audit_events_ws_time", "workspace_id", "occurred_at"),)


class NotificationPreference(Base):
    """Личные настройки доставки (FR-54, FR-86). Не меняют чужие доставки."""

    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = pk_uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    quiet_hours_start: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("22")
    )
    quiet_hours_end: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("9"))
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = version_column()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id"),
        CheckConstraint(
            "quiet_hours_start BETWEEN 0 AND 23 AND quiet_hours_end BETWEEN 0 AND 23",
            name="quiet_hours_range",
        ),
    )


class PeriodDateMarker(Base):
    """Служебная отметка «в этот день трат не было» (FR-70)."""

    __tablename__ = "no_spend_markers"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    local_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    declared_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (UniqueConstraint("workspace_id", "local_date", "declared_by"),)
