"""Импорт и экспорт (FR-63–FR-67, DATA_CONTRACT §2.6)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fintracker.db.base import Base, now_server, pk_uuid, version_column


class ImportBatch(Base):
    """Пакет импорта со снимком источника и отчётом сверки (FR-63, FR-65)."""

    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_taken_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    reconciliation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'preview'"))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()
    committed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('preview','committing','committed','cancelled','reverted','blocked')",
            name="state_allowed",
        ),
        CheckConstraint(
            "source_kind IN ('sheets_xlsx','sheets_csv','statement_csv')",
            name="source_kind_allowed",
        ),
        UniqueConstraint("workspace_id", "id"),
        # Повторный импорт неизменного снимка не создаёт новых данных (A79).
        Index(
            "uq_import_batches_snapshot",
            "workspace_id",
            "snapshot_hash",
            unique=True,
            postgresql_where=text("state = 'committed'"),
        ),
    )


class ImportRow(Base):
    """Нормализованная строка источника со стабильным ключом (FR-66)."""

    __tablename__ = "import_rows"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Стабильный ключ: источник + лист + ячейка + период + карта категорий.
    source_key: Mapped[str] = mapped_column(String(300), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_cell: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source_label: Mapped[str] = mapped_column(String(300), nullable=False)
    source_formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    granularity: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    occurred_end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    beneficiary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decision: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'new'"))
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'pending'")
    )
    conflict_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint(
            "granularity IN ('individual','daily_aggregate','period_aggregate')",
            name="granularity_allowed",
        ),
        CheckConstraint(
            "decision IN ('new','revision','skip','conflict')", name="decision_allowed"
        ),
        CheckConstraint(
            "status IN ('pending','applied','skipped','blocked')", name="status_allowed"
        ),
        UniqueConstraint("workspace_id", "batch_id", "source_key"),
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["import_batches.workspace_id", "import_batches.id"],
            name="fk_import_rows_batch",
            ondelete="CASCADE",
        ),
        Index("ix_import_rows_batch", "workspace_id", "batch_id", "status"),
    )


class SourceMapping(Base):
    """Устойчивая карта соответствия источника категориям (FR-66, A71)."""

    __tablename__ = "source_mappings"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_label: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_label: Mapped[str] = mapped_column(String(300), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    beneficiary_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = version_column()
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        UniqueConstraint("workspace_id", "source_kind", "normalized_label"),
        UniqueConstraint("workspace_id", "id"),
    )


class ImportedAggregateLink(Base):
    """Связь импортного агрегата с заменяющими подробными операциями (FR-72)."""

    __tablename__ = "imported_aggregate_links"

    id: Mapped[uuid.UUID] = pk_uuid()
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    aggregate_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    replacement_transaction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'active'"))
    approved_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = now_server()

    __table_args__ = (
        CheckConstraint("state IN ('active','cancelled')", name="state_allowed"),
        UniqueConstraint(
            "workspace_id",
            "aggregate_transaction_id",
            "replacement_transaction_id",
        ),
    )
