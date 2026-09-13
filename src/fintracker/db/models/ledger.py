"""Журнал операций: ревизии, распределения, движения счетов (DATA_CONTRACT §2.3, ADR-03).

Ревизии и AccountEntry неизменяемы. Исправление добавляет обратные и новые
движения; отчёт читает только текущую проведённую ревизию.

Все дочерние ссылки составные (workspace_id, id): связать объекты разных
бюджетов невозможно даже из сервисного SQL (SEC-09, AR-11).
"""

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
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fintracker.db.base import Base, now_server, pk_uuid, version_column

# Экономические типы операции (TZ §20).
TRANSACTION_TYPES = (
    "expense",
    "income",
    "external_funding",
    "transfer",
    "refund",
    "mixed_payment",
    "loan_received",
    "loan_principal_payment",
    "receivable_settlement",
    "adjustment",
    "legacy_unclassified_flow",
)

# Экономические роли распределений (TZ §20).
ALLOCATION_ROLES = (
    "expense",
    "receivable_increase",
    "receivable_decrease",
    "liability_decrease",
    "expense_refund",
    "receivable_reversal",
    "income",
    "external_funding",
    "interest_expense",
    "principal_repayment",
    "goal_allocation",
    "unclassified",
)


class Transaction(Base):
    """Постоянный ID операции и указатель на актуальную ревизию."""

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    source_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    current_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'posted'"))
    # Проекция для keyset-пагинации; обновляется атомарно с current_revision.
    occurred_sort_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_version: Mapped[int] = version_column()
    # Порядок добавления: время создания одинаково у записей одной транзакции,
    # поэтому «последние добавленные» опираются на последовательность (FR-07).
    created_seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("nextval('transactions_created_seq_seq'::regclass)"),
    )
    created_at: Mapped[dt.datetime] = now_server()
    updated_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("status IN ('posted','voided')", name="status_allowed"),
        CheckConstraint(
            "origin IN ('telegram_text','telegram_voice','telegram_photo','form','import',"
            "'wizard','system','api')",
            name="origin_allowed",
        ),
        CheckConstraint("current_revision > 0", name="revision_positive"),
        UniqueConstraint("workspace_id", "id"),
        # Все варианты ввода одного кандидата сходятся на одной операции (§2.6).
        Index(
            "uq_transactions_source_candidate",
            "workspace_id",
            "source_candidate_id",
            unique=True,
            postgresql_where=text("source_candidate_id IS NOT NULL"),
        ),
        Index(
            "ix_transactions_journal",
            "workspace_id",
            text("occurred_sort_date DESC"),
            text("id DESC"),
        ),
        Index("ix_transactions_created_by", "workspace_id", "created_by", "created_at"),
    )


class TransactionRevision(Base):
    """Неизменяемая ревизия операции (ADR-03)."""

    __tablename__ = "transaction_revisions"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    change_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    change_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    occurred_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    occurred_end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    occurred_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_precision: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'day'")
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    granularity: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'individual'")
    )
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    spender_person_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_voided: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(f"transaction_type IN {TRANSACTION_TYPES!r}", name="type_allowed"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("amount_minor <= 1000000000000000", name="amount_within_limit"),
        CheckConstraint("char_length(currency) = 3", name="currency_len"),
        CheckConstraint(
            "change_kind IN ('created','amended','voided','restored','note_changed',"
            "'context_changed','import_revision')",
            name="change_kind_allowed",
        ),
        CheckConstraint(
            "date_precision IN ('day','time','interval','unknown')", name="date_precision_allowed"
        ),
        CheckConstraint(
            "granularity IN ('individual','daily_aggregate','period_aggregate')",
            name="granularity_allowed",
        ),
        CheckConstraint(
            "occurred_end_date IS NULL OR occurred_end_date >= occurred_date",
            name="interval_order",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("note IS NULL OR char_length(note) <= 2000", name="note_length"),
        UniqueConstraint("workspace_id", "transaction_id", "revision"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "transaction_id"],
            ["transactions.workspace_id", "transactions.id"],
            name="fk_transaction_revisions_transaction",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "spender_person_id"],
            ["people.workspace_id", "people.id"],
            name="fk_transaction_revisions_spender",
            ondelete="RESTRICT",
        ),
        Index("ix_transaction_revisions_txn", "workspace_id", "transaction_id", "revision"),
        Index(
            "ix_transaction_revisions_occurred",
            "workspace_id",
            "occurred_date",
            "transaction_id",
        ),
    )


class Allocation(Base):
    """Часть денежного события с экономической ролью (TZ §20, R05)."""

    __tablename__ = "allocations"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Стабильный ключ строки распределения — переживает исправления (для возвратов).
    stable_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    economic_role: Mapped[str] = mapped_column(String(32), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    beneficiary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unit_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related_object_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    related_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    line_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(f"economic_role IN {ALLOCATION_ROLES!r}", name="role_allowed"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "transaction_id", "revision"],
            [
                "transaction_revisions.workspace_id",
                "transaction_revisions.transaction_id",
                "transaction_revisions.revision",
            ],
            name="fk_allocations_revision",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_allocations_category",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "beneficiary_id"],
            ["beneficiaries.workspace_id", "beneficiaries.id"],
            name="fk_allocations_beneficiary",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_allocations_category", "workspace_id", "category_id", "transaction_id", "revision"
        ),
        Index(
            "ix_allocations_beneficiary",
            "workspace_id",
            "beneficiary_id",
            "transaction_id",
            "revision",
        ),
        Index("ix_allocations_stable_line", "workspace_id", "stable_line_id"),
        # Ключ соединения по ревизии: без него триггеры инвариантов и отчёты
        # сканируют таблицу целиком (ADR-16, NFR-06).
        Index("ix_allocations_revision", "workspace_id", "transaction_id", "revision"),
    )


class CashLeg(Base):
    """Направление реального внешнего потока денег (§2.3)."""

    __tablename__ = "cash_legs"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    coverage_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("signed_minor <> 0", name="signed_not_zero"),
        CheckConstraint(
            "coverage_mode IN ('tracked','reference','unknown','included_in_opening')",
            name="coverage_mode_allowed",
        ),
        CheckConstraint(
            "(coverage_mode = 'unknown' AND account_id IS NULL) OR "
            "(coverage_mode <> 'unknown' AND account_id IS NOT NULL)",
            name="unknown_has_no_account",
        ),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "transaction_id", "revision"],
            [
                "transaction_revisions.workspace_id",
                "transaction_revisions.transaction_id",
                "transaction_revisions.revision",
            ],
            name="fk_cash_legs_revision",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_cash_legs_account",
            ondelete="RESTRICT",
        ),
        Index("ix_cash_legs_revision", "workspace_id", "transaction_id", "revision"),
    )


class FinancialEffect(Base):
    """Действующий финансовый эффект операции (ADR-03).

    Исправление создаёт новый эффект и помечает предыдущий заменённым;
    движения счетов старого эффекта обращаются, а не переписываются.
    """

    __tablename__ = "financial_effects"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    replaced_effect_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), unique=True, nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "transaction_id", "source_revision"),
        Index(
            "ix_financial_effects_transaction",
            "workspace_id",
            "transaction_id",
            "source_revision",
        ),
        # У операции не более одного активного эффекта.
        Index(
            "uq_financial_effects_active",
            "workspace_id",
            "transaction_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "transaction_id"],
            ["transactions.workspace_id", "transactions.id"],
            name="fk_financial_effects_transaction",
            ondelete="CASCADE",
        ),
    )


class AccountEntry(Base):
    """Неизменяемое движение по счёту (ADR-03). Только для tracked охвата."""

    __tablename__ = "account_entries"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    effect_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    opening_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    signed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_at: Mapped[dt.datetime] = now_server()
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), unique=True, nullable=True
    )
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("signed_minor <> 0", name="signed_not_zero"),
        CheckConstraint(
            "(effect_id IS NOT NULL AND transaction_id IS NOT NULL AND revision IS NOT NULL) "
            "OR opening_adjustment_id IS NOT NULL",
            name="entry_source_present",
        ),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "account_id"],
            ["accounts.workspace_id", "accounts.id"],
            name="fk_account_entries_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "effect_id"],
            ["financial_effects.workspace_id", "financial_effects.id"],
            name="fk_account_entries_effect",
            ondelete="RESTRICT",
        ),
        Index("ix_account_entries_balance", "workspace_id", "account_id", "effective_date", "id"),
        Index("ix_account_entries_effect", "workspace_id", "effect_id"),
    )


class TransactionLink(Base):
    """Связь между операциями: возврат, перевод, возмещение, замещение (§2.3)."""

    __tablename__ = "transaction_links"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    link_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_stable_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    target_stable_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "link_type IN ('refund_of','transfer_pair','settles_receivable','replaces_aggregate',"
            "'duplicate_of','settles_occurrence')",
            name="link_type_allowed",
        ),
        CheckConstraint("status IN ('active','cancelled')", name="status_allowed"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "source_transaction_id <> target_transaction_id", name="distinct_transactions"
        ),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_transaction_links_source", "workspace_id", "source_transaction_id", "link_type"),
        Index("ix_transaction_links_target", "workspace_id", "target_transaction_id", "link_type"),
    )


class Receivable(Base):
    """Требование по совместной покупке (FR-30, P0)."""

    __tablename__ = "receivables"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    counterparty_person_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    counterparty_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    original_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outstanding_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    origin_stable_line_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("original_minor > 0", name="original_positive"),
        CheckConstraint("outstanding_minor >= 0", name="outstanding_non_negative"),
        CheckConstraint("outstanding_minor <= original_minor", name="outstanding_within_original"),
        CheckConstraint("status IN ('open','settled','written_off')", name="status_allowed"),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "origin_stable_line_id"),
    )


class ReceivableEntry(Base):
    __tablename__ = "receivable_entries"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    receivable_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    effect_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    change_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("change_minor <> 0", name="change_not_zero"),
        CheckConstraint(
            "kind IN ('increase','settlement','reversal','write_off')", name="kind_allowed"
        ),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "receivable_id", "effect_id"),
        ForeignKeyConstraint(
            ["workspace_id", "receivable_id"],
            ["receivables.workspace_id", "receivables.id"],
            name="fk_receivable_entries_receivable",
            ondelete="CASCADE",
        ),
    )


class OpeningAdjustment(Base):
    """Начальный остаток или техническая корректировка счёта (FR-71)."""

    __tablename__ = "opening_adjustments"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("kind IN ('opening_balance','reconciliation')", name="kind_allowed"),
        UniqueConstraint("workspace_id", "id"),
        Index("ix_opening_adjustments_account", "workspace_id", "account_id", "effective_date"),
    )
