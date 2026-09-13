"""Порядок добавления операций

Revision ID: 0006_tx_seq
Revises: 0005_invite

FR-07 требует показывать «последние добавленные» записи. Время создания для
этого недостаточно: операции, записанные в одной транзакции, получают
одинаковый ``now()``, и порядок становится неопределённым. Монотонная
последовательность даёт устойчивый порядок добавления, не зависящий от
часов и от даты самой операции.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES

revision: str = "0006_tx_seq"
down_revision: str | None = "0005_invite"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE transactions ADD COLUMN created_seq BIGSERIAL NOT NULL")
    op.execute(
        "CREATE INDEX ix_transactions_created_seq ON transactions (workspace_id, created_seq DESC)"
    )
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE transactions_created_seq_seq TO {roles}")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transactions_created_seq")
    op.execute("ALTER TABLE transactions DROP COLUMN IF EXISTS created_seq")
