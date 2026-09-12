"""Категории, правила классификации и метки (DATA_CONTRACT §2.2)."""

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


class Category(Base):
    """Категория со стабильным ID; переименование сохраняет историю (FR-21, FR-22)."""

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Флаги планирования (FR-21): свойства бюджета, не оценка модели.
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    planning_flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "parent_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_categories_parent_same_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="no_self_parent"),
        CheckConstraint("char_length(btrim(name)) > 0", name="name_not_blank"),
        # Активное имя уникально среди детей одного родителя, корень через
        # NULLS NOT DISTINCT (DATA_CONTRACT §2.2).
        Index(
            "uq_categories_active_name",
            "workspace_id",
            "parent_id",
            "normalized_name",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_categories_ws_parent", "workspace_id", "parent_id", "sort_order"),
    )


class CategoryAlias(Base):
    __tablename__ = "category_aliases"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "normalized_alias"),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_category_aliases_category",
            ondelete="CASCADE",
        ),
    )


class CategoryMergeMap(Base):
    """Соответствие старых ID новым после объединения (FR-22)."""

    __tablename__ = "category_merge_map"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_category_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_category_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    merged_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    affected_transactions: Mapped[int] = mapped_column(Integer, nullable=False)
    merged_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (UniqueConstraint("workspace_id", "source_category_id"),)


class ClassificationRule(Base):
    """Личное или общее правило классификации внутри бюджета (FR-23, FR-24)."""

    __tablename__ = "classification_rules"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_membership_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    specificity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    action: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("scope IN ('workspace','member')", name="scope_allowed"),
        CheckConstraint(
            "(scope = 'workspace' AND owner_membership_id IS NULL) OR "
            "(scope = 'member' AND owner_membership_id IS NOT NULL)",
            name="scope_owner_consistency",
        ),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_classification_rules_ws", "workspace_id", "scope", "priority"),
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(60), nullable=False)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_tags_active_name",
            "workspace_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )


class TransactionTag(Base):
    """Связь метки с конкретной ревизией операции (DATA_CONTRACT §2.2)."""

    __tablename__ = "transaction_tags"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "transaction_id", "revision", "tag_id"),
        ForeignKeyConstraint(
            ["workspace_id", "tag_id"],
            ["tags.workspace_id", "tags.id"],
            name="fk_transaction_tags_tag",
            ondelete="RESTRICT",
        ),
        Index("ix_transaction_tags_lookup", "workspace_id", "tag_id", "transaction_id"),
    )


class Account(Base):
    """Счёт с режимом охвата full_tracking/reference (ADR-03, R01)."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    account_type: Mapped[str] = mapped_column(String(24), nullable=False)
    balance_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'posted'")
    )
    # Начальная точка: либо начало локальной даты, либо точный момент (§2.3).
    opening_cutoff_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    opening_cutoff_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    opening_cutoff_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_liquid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    included_in_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("mode IN ('full_tracking','reference')", name="mode_allowed"),
        CheckConstraint(
            "account_type IN ('cash','card','bank','savings','credit_card','other')",
            name="type_allowed",
        ),
        CheckConstraint("balance_kind IN ('posted','available')", name="balance_kind_allowed"),
        CheckConstraint(
            "opening_cutoff_kind IS NULL OR opening_cutoff_kind IN ('date_start','moment')",
            name="opening_cutoff_kind_allowed",
        ),
        CheckConstraint(
            "(opening_cutoff_kind IS NULL AND opening_cutoff_date IS NULL "
            "AND opening_cutoff_at IS NULL) "
            "OR (opening_cutoff_kind = 'date_start' AND opening_cutoff_date IS NOT NULL) "
            "OR (opening_cutoff_kind = 'moment' AND opening_cutoff_at IS NOT NULL)",
            name="opening_cutoff_consistency",
        ),
        # P0 не принимает кредитную карту как обычный положительный счёт (FR-31).
        CheckConstraint(
            "account_type <> 'credit_card' OR mode = 'reference'",
            name="credit_card_reference_only_p0",
        ),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "id", "currency"),
        Index(
            "uq_accounts_active_name",
            "workspace_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )
