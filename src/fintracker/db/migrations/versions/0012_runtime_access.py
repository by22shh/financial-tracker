"""Access recovery discovers IDs without migration credentials (V-05).

Revision ID: 0012_access
Revises: 0011_reply

Only discovery needs a privileged function: mutations use the existing
workspace-scoped RLS policies under the limited worker role. No financial
fields, arbitrary query, mutation function or BYPASSRLS grant is exposed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_access"
down_revision: str | None = "0011_reply"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION access_recovery_workspaces()
        RETURNS TABLE (id UUID)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$ SELECT w.id FROM workspaces w ORDER BY w.id $$
    """)
    op.execute("REVOKE ALL ON FUNCTION access_recovery_workspaces() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION access_recovery_workspaces() TO fintracker_worker")


def downgrade() -> None:
    op.execute("DROP FUNCTION access_recovery_workspaces()")
