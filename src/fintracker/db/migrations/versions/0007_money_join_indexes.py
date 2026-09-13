"""Индексы соединений по ревизии и дате операции (NFR-10)

Revision ID: 0007_money_idx
Revises: 0006_tx_seq

Измеренное ограничение (AR-34, NFR-06): отчёт по 50 000 операций занимал
94 с при требуемых 2 с, а фиксация партии импорта — минуты. Причина —
отсутствие индексов по ключу (workspace_id, transaction_id, revision):
отложенные триггеры денежных инвариантов и соединения журнала выполняли
последовательный скан распределений и денежных частей на каждую операцию.

Изменение архитектуры выполняется по измеренному основанию (ADR-16):
добавляются только индексы, схема и модель данных не меняются.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_money_idx"
down_revision: str | None = "0006_tx_seq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_allocations_revision",
        "allocations",
        "(workspace_id, transaction_id, revision)",
    ),
    (
        "ix_cash_legs_revision",
        "cash_legs",
        "(workspace_id, transaction_id, revision)",
    ),
    (
        "ix_transaction_revisions_occurred",
        "transaction_revisions",
        "(workspace_id, occurred_date, transaction_id)",
    ),
    (
        "ix_account_entries_effect",
        "account_entries",
        "(workspace_id, effect_id)",
    ),
    (
        "ix_financial_effects_transaction",
        "financial_effects",
        "(workspace_id, transaction_id, source_revision)",
    ),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {columns}")


def downgrade() -> None:
    for name, _table, _columns in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
