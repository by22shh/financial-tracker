"""Persist long journal-query continuations for Telegram callbacks.

Revision ID: 0018_history_query
Revises: 0017_media_retention
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES, user_policy_sql

revision: str = "0018_history_query"
down_revision: str | None = "0017_media_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE history_query_states (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            workspace_id UUID NOT NULL,
            token VARCHAR(16) NOT NULL,
            query TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_history_query_states_user_workspace_token
                UNIQUE (user_id, workspace_id, token)
        );
        CREATE INDEX ix_history_query_states_expiry ON history_query_states (expires_at);
        """
    )
    for statement in user_policy_sql("history_query_states"):
        op.execute(statement)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON history_query_states TO {', '.join(RUNTIME_ROLES)}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS history_query_states")
