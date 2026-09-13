"""Аналитические снимки, запуски анализа и рекомендации (FR-73–FR-76, ADR-09)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fintracker.db.base import Base, now_server, pk_uuid, version_column


class AnalyticsSnapshot(Base):
    """Числовое основание любого AI-утверждения (AI-08, ADR-09)."""

    __tablename__ = "analytics_snapshots"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    period_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    date_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    date_to_exclusive: Mapped[dt.date] = mapped_column(Date, nullable=False)
    filter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    # Вектор версий основы: деньги, план, календарь, справочники, полнота.
    revision_vector: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coverage_status: Mapped[str] = mapped_column(String(24), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("date_from < date_to_exclusive", name="range_order"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_analytics_snapshots_ws", "workspace_id", "computed_at"),
    )


class AnalysisRun(Base):
    """Логический запуск периодического анализа (FR-73). Повтор не дублирует обзор."""

    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    logical_key: Mapped[str] = mapped_column(String(200), nullable=False)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'pending'")
    )
    profile_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    requested_model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    returned_model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    service_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Logical run survives workers; each provider attempt has its own identity.
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attempt_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    attempt_lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    summary: Mapped[str] = mapped_column(String, nullable=False, server_default=text("''"))
    abstained_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[dt.datetime] = now_server()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "run_kind IN ('weekly_review','plan_preparation','period_closing','on_demand')",
            name="run_kind_allowed",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','no_new_data','failed','fallback')",
            name="status_allowed",
        ),
        UniqueConstraint("workspace_id", "logical_key"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_analysis_runs_ws", "workspace_id", "started_at"),
    )


class Recommendation(Base):
    """Карточка рекомендации (FR-75). Сумма и основание проверяются сервером."""

    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(40), nullable=False)
    observation: Mapped[str] = mapped_column(String(1000), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Метрики снимка, на которых построено утверждение (AI-08).
    metric_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    estimated_effect_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    effect_formula: Mapped[str | None] = mapped_column(String(500), nullable=True)
    effect_unavailable_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    horizon_period_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    conditions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Группа взаимоисключающих вариантов: их эффекты не складываются (A135).
    alternative_group: Mapped[str | None] = mapped_column(String(60), nullable=True)
    stable_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revision_vector: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'proposed'")
    )
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','stale','applied','withdrawn')", name="status_allowed"
        ),
        CheckConstraint(
            "direction IN ('flexible_spend','repeated_overspend','known_recurring',"
            "'irregular_payments','savings_goals','budget_imbalance')",
            name="direction_allowed",
        ),
        CheckConstraint(
            "action_kind IN ('reduce_flexible','adjust_plan','check_tariff','start_fund',"
            "'adjust_contribution','reallocate_limit','review_completeness')",
            name="action_kind_allowed",
        ),
        CheckConstraint(
            "estimated_effect_minor IS NOT NULL OR effect_unavailable_reason IS NOT NULL",
            name="effect_or_reason",
        ),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["analysis_runs.workspace_id", "analysis_runs.id"],
            name="fk_recommendations_run",
            ondelete="CASCADE",
        ),
        Index("ix_recommendations_ws_status", "workspace_id", "status", "priority"),
    )


class RecommendationFeedback(Base):
    """Личная обратная связь участника (FR-76). Не меняет общий план сама по себе."""

    __tablename__ = "recommendation_feedback"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    recommendation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'personal'")
    )
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    snooze_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    review_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    observation_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "decision IN ('chosen','changed','snoozed','rejected','done','mute_direction')",
            name="decision_allowed",
        ),
        CheckConstraint("scope IN ('personal','workspace')", name="scope_allowed"),
        UniqueConstraint("workspace_id", "recommendation_id", "membership_id"),
        UniqueConstraint("workspace_id", "id"),
    )


class AnalysisPreference(Base):
    """Общий календарь анализа бюджета (FR-73). Меняет администратор."""

    __tablename__ = "analysis_preferences"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    weekly_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    weekly_weekday: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("6"))
    weekly_hour: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("19"))
    plan_preparation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    closing_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    muted_directions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    version: Mapped[int] = version_column()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("weekly_weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("weekly_hour BETWEEN 0 AND 23", name="hour_range"),
    )


class DirectionMute(Base):
    """Отключение направления рекомендаций для статьи (FR-76)."""

    __tablename__ = "recommendation_direction_mutes"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(40), nullable=False)
    stable_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "membership_id",
            "direction",
            "stable_line_id",
            postgresql_nulls_not_distinct=True,
        ),
    )
