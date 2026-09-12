"""Обязательства, цели, резервы, полнота и сверка (DATA_CONTRACT §2.5)."""

from __future__ import annotations

import datetime as dt
import uuid

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
from sqlalchemy.orm import Mapped, mapped_column

from fintracker.db.base import Base, now_server, pk_uuid, version_column


class ScheduledItem(Base):
    """Стабильное расписание платежа или дохода (FR-45, FR-47)."""

    __tablename__ = "scheduled_items"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    current_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = version_column()
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("direction IN ('payment','income')", name="direction_allowed"),
        UniqueConstraint("workspace_id", "id"),
    )


class ScheduleVersion(Base):
    """Версия правила расписания; частота независима от бюджетного периода."""

    __tablename__ = "schedule_versions"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schedule_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    rule_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_interval: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    anchor_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_last_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    ends_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    expected_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_min_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_max_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    beneficiary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fund_goal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reminder_days_before: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "rule_kind IN ('once','daily','weekly','monthly','yearly')", name="rule_kind_allowed"
        ),
        CheckConstraint("rule_interval >= 1", name="interval_positive"),
        CheckConstraint(
            "day_of_month IS NULL OR day_of_month BETWEEN 1 AND 31", name="day_of_month_range"
        ),
        UniqueConstraint("workspace_id", "schedule_id", "version"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "schedule_id"],
            ["scheduled_items.workspace_id", "scheduled_items.id"],
            name="fk_schedule_versions_schedule",
            ondelete="CASCADE",
        ),
    )


class Occurrence(Base):
    """Экземпляр ожидания платежа/дохода (R04, DATA_CONTRACT §2.5)."""

    __tablename__ = "occurrences"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schedule_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schedule_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurrence_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    original_due_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    due_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expected_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settled_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'planned'"))
    change_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "state IN ('planned','partially_settled','settled','skipped','cancelled','superseded')",
            name="state_allowed",
        ),
        CheckConstraint("settled_minor >= 0", name="settled_non_negative"),
        CheckConstraint(
            "expected_minor IS NULL OR settled_minor <= expected_minor",
            name="settled_within_expected",
        ),
        UniqueConstraint(
            "workspace_id",
            "schedule_id",
            "original_due_date",
            "occurrence_slot",
        ),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "schedule_id"],
            ["scheduled_items.workspace_id", "scheduled_items.id"],
            name="fk_occurrences_schedule",
            ondelete="CASCADE",
        ),
        Index(
            "ix_occurrences_due",
            "workspace_id",
            "due_date",
            "id",
            postgresql_where=text("state IN ('planned','partially_settled')"),
        ),
    )


class OccurrenceSettlement(Base):
    """Связь факта с конкретным экземпляром обязательства (FR-46)."""

    __tablename__ = "occurrence_settlements"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    effect_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stable_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("status IN ('active','cancelled')", name="status_allowed"),
        UniqueConstraint("workspace_id", "occurrence_id", "effect_id"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "occurrence_id"],
            ["occurrences.workspace_id", "occurrences.id"],
            name="fk_occurrence_settlements_occurrence",
            ondelete="CASCADE",
        ),
    )


class Goal(Base):
    """Цель накоплений (FR-49). Резерв отделён от подтверждённого счёта."""

    __tablename__ = "goals"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'goal'"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    target_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    allocated_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    due_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    contribution_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    contribution_frequency: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'per_period'")
    )
    remaining_contributions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    version: Mapped[int] = version_column()
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("kind IN ('goal','fund')", name="kind_allowed"),
        CheckConstraint("status IN ('active','paused','reached','closed')", name="status_allowed"),
        CheckConstraint(
            "contribution_frequency IN ('per_period','monthly','weekly','custom')",
            name="frequency_allowed",
        ),
        CheckConstraint("allocated_minor >= 0", name="allocated_non_negative"),
        UniqueConstraint("workspace_id", "id"),
    )


class GoalMovement(Base):
    """Выделение, использование и освобождение средств цели (FR-50, FR-51)."""

    __tablename__ = "goal_movements"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    goal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    change_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effect_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    period_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("change_minor <> 0", name="change_not_zero"),
        CheckConstraint("kind IN ('allocate','use','release','adjust')", name="kind_allowed"),
        UniqueConstraint("workspace_id", "id"),
        # Каждая связь с финансовым эффектом уникальна (§2.5).
        Index(
            "uq_goal_movements_effect",
            "workspace_id",
            "goal_id",
            "effect_id",
            "kind",
            unique=True,
            postgresql_where=text("effect_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "goal_id"],
            ["goals.workspace_id", "goals.id"],
            name="fk_goal_movements_goal",
            ondelete="CASCADE",
        ),
    )


class CashReservation(Base):
    """Непересекающаяся защищённая сумма (§2.5, B7)."""

    __tablename__ = "cash_reservations"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    basis_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    occurrence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = now_server()
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("basis_kind IN ('goal','occurrence','manual')", name="basis_kind_allowed"),
        # Одна сумма не вычитается из доступного ресурса дважды (§2.5).
        CheckConstraint(
            "(basis_kind = 'goal' AND goal_id IS NOT NULL AND occurrence_id IS NULL) OR "
            "(basis_kind = 'occurrence' AND occurrence_id IS NOT NULL AND goal_id IS NULL) OR "
            "(basis_kind = 'manual' AND goal_id IS NULL AND occurrence_id IS NULL)",
            name="basis_exclusive",
        ),
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_cash_reservations_goal",
            "workspace_id",
            "goal_id",
            unique=True,
            postgresql_where=text("is_active AND goal_id IS NOT NULL"),
        ),
        Index(
            "uq_cash_reservations_occurrence",
            "workspace_id",
            "occurrence_id",
            unique=True,
            postgresql_where=text("is_active AND occurrence_id IS NOT NULL"),
        ),
    )


class CoverageRecord(Base):
    """Отметка полноты учёта по области (FR-69)."""

    __tablename__ = "coverage_records"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    period_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    date_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    basis: Mapped[str] = mapped_column(String(200), nullable=False)
    basis_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    declared_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('period','account','person','date_range')", name="scope_kind_allowed"
        ),
        CheckConstraint(
            "status IN ('incomplete','reconciled_source','confirmed_complete')",
            name="status_allowed",
        ),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_coverage_records_ws", "workspace_id", "scope_kind", "period_id"),
    )


class Reconciliation(Base):
    """Сверка остатка счёта (FR-71)."""

    __tablename__ = "reconciliations"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    cutoff_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    balance_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    difference_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'open'"))
    basis_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    author_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("balance_kind IN ('posted','available')", name="balance_kind_allowed"),
        CheckConstraint("status IN ('open','accepted','superseded')", name="status_allowed"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_reconciliations_account", "workspace_id", "account_id", "cutoff_date"),
    )
