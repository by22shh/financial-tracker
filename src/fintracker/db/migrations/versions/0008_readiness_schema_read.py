"""Чтение версии схемы для readiness

Revision ID: 0008_ready
Revises: 0007_money_idx

Readiness обязан показывать совместимость схемы (OPS-02), но runtime роли не
имели права читать alembic_version: проверка всегда возвращала «не готов».
Право на изменение таблицы остаётся только у владельца миграций.
"""

# ruff: noqa: S608 — имена ролей берутся из фиксированной константы RUNTIME_ROLES.

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES

revision: str = "0008_ready"
down_revision: str | None = "0007_money_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(f"GRANT SELECT ON alembic_version TO {roles}")


def downgrade() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(f"REVOKE SELECT ON alembic_version FROM {roles}")
