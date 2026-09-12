"""Календарь периодов, планы и переносы (DATA_CONTRACT §2.2, ADR-07)."""

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
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fintracker.db.base import Base, now_server, pk_uuid, version_column


class PeriodPolicyRow(Base):
    """Версия правила повторения периодов (FR-90–FR-91, FR-94)."""

    __tablename__ = "period_policies"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    anchor_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    anchor_day: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    interval: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    first_end_exclusive: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Номер первого периода этой версии в общей последовательности бюджета.
    base_sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    superseded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("mode IN ('calendar_months','fixed_days')", name="mode_allowed"),
        CheckConstraint("interval >= 1", name="interval_positive"),
        CheckConstraint("anchor_day BETWEEN 1 AND 31", name="anchor_day_range"),
        CheckConstraint("first_end_exclusive > anchor_date", name="first_end_after_anchor"),
        UniqueConstraint("workspace_id", "version"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_period_policies_ws_effective", "workspace_id", "effective_from"),
    )


class RecurringPlanTemplate(Base):
    """Утверждённый повторяемый шаблон плана (FR-93). Фактов в шаблоне нет."""

    __tablename__ = "recurring_plan_templates"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    approval_actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Лимиты строк: [{category_id, beneficiary_id, limit_minor, rollover_mode}]
    lines: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    income_rule: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    goal_rules: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    overall_limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "version"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_plan_templates_ws_effective", "workspace_id", "effective_from"),
    )


class BudgetPeriod(Base):
    """Период бюджета ``[start_date, end_exclusive)`` (FR-27, ADR-07)."""

    __tablename__ = "budget_periods"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_exclusive: Mapped[dt.date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    is_transition: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    completeness: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'incomplete'")
    )
    closing_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("start_date < end_exclusive", name="start_before_end"),
        CheckConstraint("state IN ('planned','open','ended')", name="state_allowed"),
        CheckConstraint(
            "completeness IN ('incomplete','reconciled_source','confirmed_complete')",
            name="completeness_allowed",
        ),
        UniqueConstraint("workspace_id", "start_date"),
        UniqueConstraint("workspace_id", "policy_id", "sequence"),
        UniqueConstraint("workspace_id", "id"),
        # Отсутствие пересечений внутри одного бюджета (btree_gist + daterange).
        ExcludeConstraint(
            ("workspace_id", "="),
            (text("daterange(start_date, end_exclusive, '[)')"), "&&"),
            name="budget_periods_no_overlap",
            using="gist",
        ),
        Index("ix_budget_periods_ws_dates", "workspace_id", "start_date", "end_exclusive"),
    )


class BudgetVersion(Base):
    """Версия плана периода: baseline и working (FR-36)."""

    __tablename__ = "budget_versions"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plan_status: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    template_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    overall_limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deficit_accepted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    deficit_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("kind IN ('baseline','working')", name="kind_allowed"),
        CheckConstraint(
            "plan_status IN ('draft','approved','needs_review')", name="plan_status_allowed"
        ),
        CheckConstraint(
            "origin IN ('wizard','template','manual','proposal','transition')",
            name="origin_allowed",
        ),
        UniqueConstraint("workspace_id", "period_id", "kind", "version"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "period_id"],
            ["budget_periods.workspace_id", "budget_periods.id"],
            name="fk_budget_versions_period",
            ondelete="CASCADE",
        ),
        Index("ix_budget_versions_current", "workspace_id", "period_id", "kind", "version"),
    )


class BudgetLine(Base):
    """Строка плана: категория + получатель (FR-35). ``limit_minor`` NULL — лимит не задан."""

    __tablename__ = "budget_lines"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    budget_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Стабильный ключ строки, переживающий переименование и объединение.
    stable_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    beneficiary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rollover_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'none'")
    )
    is_protected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("limit_minor IS NULL OR limit_minor >= 0", name="limit_non_negative"),
        CheckConstraint(
            "rollover_mode IN ('none','positive_only','signed')", name="rollover_mode_allowed"
        ),
        UniqueConstraint(
            "workspace_id",
            "budget_version_id",
            "category_id",
            "beneficiary_id",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "budget_version_id"],
            ["budget_versions.workspace_id", "budget_versions.id"],
            name="fk_budget_lines_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_budget_lines_category",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "beneficiary_id"],
            ["beneficiaries.workspace_id", "beneficiaries.id"],
            name="fk_budget_lines_beneficiary",
            ondelete="RESTRICT",
        ),
        Index("ix_budget_lines_stable", "workspace_id", "stable_line_id"),
    )


class IncomePlan(Base):
    """План дохода периода (FR-85). План не создаёт факта поступления."""

    __tablename__ = "income_plans"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    period_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    precision: Mapped[str] = mapped_column(String(16), nullable=False)
    basis: Mapped[str] = mapped_column(String(24), nullable=False)
    monthly_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    period_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    min_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    detailed_by_sources: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    unknown_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = version_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("precision IN ('exact','estimate','range','unknown')", name="precision_ok"),
        CheckConstraint(
            "basis IN ('monthly_total','period_total','schedule','unknown')", name="basis_ok"
        ),
        UniqueConstraint("workspace_id", "period_id"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_income_plans_ws", "workspace_id", "period_id"),
    )


class IncomeSource(Base):
    __tablename__ = "income_sources"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    income_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    expected_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "income_plan_id"],
            ["income_plans.workspace_id", "income_plans.id"],
            name="fk_income_sources_plan",
            ondelete="CASCADE",
        ),
    )


class BudgetTransfer(Base):
    """Перераспределение лимита между строками (FR-37). Доход не растёт."""

    __tablename__ = "budget_transfers"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_stable_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    to_stable_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    basis_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("from_stable_line_id <> to_stable_line_id", name="distinct_lines"),
        UniqueConstraint("workspace_id", "id"),
    )


class Rollover(Base):
    """Перенос остатка между периодами (FR-39). Идемпотентен по логическому ключу."""

    __tablename__ = "rollovers"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_period_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    destination_period_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stable_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'proposed'")
    )
    basis_completeness: Mapped[str] = mapped_column(String(24), nullable=False)
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("mode IN ('positive_only','signed')", name="mode_allowed"),
        CheckConstraint("status IN ('proposed','accepted','superseded')", name="status_allowed"),
        UniqueConstraint(
            "workspace_id",
            "source_period_id",
            "destination_period_id",
            "stable_line_id",
        ),
        UniqueConstraint("workspace_id", "id"),
    )
