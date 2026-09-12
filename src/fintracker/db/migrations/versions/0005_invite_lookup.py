"""Узкая функция разрешения кода приглашения

Revision ID: 0005_invite
Revises: 0004_admin_trigger

Приглашение проверяется до того, как известен бюджет, поэтому обычная
изоляция по app.workspace_id закрывает поиск. ADR-06 разрешает отдельную
SECURITY DEFINER функцию для узкой подтверждённой необходимости.

Функция принимает только проверочное значение секрета (HMAC) и возвращает
минимум, разрешённый FR-78: идентификатор бюджета, его название, роль
приглашения и признаки действительности. Финансовые данные, список
участников и суммы не раскрываются.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES

revision: str = "0005_invite"
down_revision: str | None = "0004_admin_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_invite_by_digest(p_digest TEXT)
RETURNS TABLE (
    invite_id UUID,
    workspace_id UUID,
    workspace_name TEXT,
    workspace_state TEXT,
    role TEXT,
    expires_at TIMESTAMPTZ,
    max_uses INT,
    used_uses INT,
    revoked_at TIMESTAMPTZ
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT i.id, i.workspace_id, w.name::TEXT, w.state::TEXT, i.role::TEXT,
           i.expires_at, i.max_uses, i.used_uses, i.revoked_at
      FROM budget_invites i
      JOIN workspaces w ON w.id = i.workspace_id
     WHERE i.secret_digest = p_digest
     LIMIT 1;
$$;
"""

MEMBERSHIP_FUNCTION = """
CREATE OR REPLACE FUNCTION self_membership_state(p_workspace_id UUID, p_user_id UUID)
RETURNS TABLE (status TEXT, role TEXT, rejoin_blocked BOOLEAN, member_count INT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT COALESCE(m.status, '')::TEXT,
           COALESCE(m.role, '')::TEXT,
           COALESCE(m.rejoin_blocked, false),
           (SELECT COUNT(*)::INT FROM memberships a
             WHERE a.workspace_id = p_workspace_id AND a.status = 'active')
      FROM (SELECT 1) t
      LEFT JOIN memberships m
        ON m.workspace_id = p_workspace_id AND m.user_id = p_user_id;
$$;
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute(MEMBERSHIP_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_invite_by_digest(TEXT) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION self_membership_state(UUID, UUID) FROM PUBLIC")
    for role in RUNTIME_ROLES:
        op.execute(f"GRANT EXECUTE ON FUNCTION resolve_invite_by_digest(TEXT) TO {role}")
        op.execute(f"GRANT EXECUTE ON FUNCTION self_membership_state(UUID, UUID) TO {role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_invite_by_digest(TEXT)")
    op.execute("DROP FUNCTION IF EXISTS self_membership_state(UUID, UUID)")
