"""Ожидаемый ввод участника после нажатия кнопки

Revision ID: 0015_pending
Revises: 0014_cards

Кнопки «Переименовать», «Задать лимит», «Добавить цель», «Создать платёж» и
«Оплачено» обещают продолжение диалога, но следующее сообщение участника
некуда было отнести: обещанное действие не выполнялось (G-13…G-16).

Строка личная: она принадлежит участнику и его бюджету, живёт ограниченное
время и не содержит финансовых сумм сверх самого введённого значения.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES, user_policy_sql

revision: str = "0015_pending"
down_revision: str | None = "0014_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = """
CREATE TABLE pending_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id UUID,
    kind VARCHAR(32) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_pending_actions_user UNIQUE (user_id)
);

CREATE INDEX ix_pending_actions_expiry ON pending_actions (expires_at);
"""


def upgrade() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(TABLE)
    for statement in user_policy_sql("pending_actions"):
        op.execute(statement)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON pending_actions TO {roles}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pending_actions")
