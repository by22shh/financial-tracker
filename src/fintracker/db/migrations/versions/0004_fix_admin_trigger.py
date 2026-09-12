"""Исправление триггера проверки единственного администратора

Revision ID: 0004_admin_trigger
Revises: 0003_identity

Дефект: функция обращалась к NEW.workspace_id до проверки TG_TABLE_NAME,
поэтому срабатывание на таблице workspaces падало с UndefinedColumn.
Теперь источник идентификатора выбирается по имени таблицы, а операция
DELETE использует OLD.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_admin_trigger"
down_revision: str | None = "0003_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIXED_FUNCTION = """
CREATE OR REPLACE FUNCTION check_workspace_admin_exactly_one() RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    ws_id     UUID;
    ws_state  TEXT;
    ws_admin  UUID;
    admin_cnt INT;
    admin_uid UUID;
BEGIN
    IF TG_TABLE_NAME = 'workspaces' THEN
        ws_id := COALESCE(NEW.id, OLD.id);
    ELSIF TG_OP = 'DELETE' THEN
        ws_id := OLD.workspace_id;
    ELSE
        ws_id := NEW.workspace_id;
    END IF;

    IF ws_id IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT w.state, w.admin_user_id INTO ws_state, ws_admin
      FROM workspaces w WHERE w.id = ws_id;
    IF NOT FOUND OR ws_state <> 'active' THEN
        RETURN NULL;
    END IF;

    SELECT COUNT(*) INTO admin_cnt
      FROM memberships m
     WHERE m.workspace_id = ws_id AND m.role = 'admin' AND m.status = 'active';

    SELECT m.user_id INTO admin_uid
      FROM memberships m
     WHERE m.workspace_id = ws_id AND m.role = 'admin' AND m.status = 'active'
     LIMIT 1;

    IF admin_cnt <> 1 THEN
        RAISE EXCEPTION
          'У действующего бюджета должен быть ровно один администратор, найдено %', admin_cnt
          USING ERRCODE = 'check_violation';
    END IF;
    IF ws_admin IS DISTINCT FROM admin_uid THEN
        RAISE EXCEPTION 'workspaces.admin_user_id не совпадает с действующим членством admin'
          USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;
"""

PREVIOUS_FUNCTION = """
CREATE OR REPLACE FUNCTION check_workspace_admin_exactly_one() RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    ws_id     UUID;
    ws_state  TEXT;
    ws_admin  UUID;
    admin_cnt INT;
    admin_uid UUID;
BEGIN
    ws_id := COALESCE(NEW.workspace_id, OLD.workspace_id);
    IF TG_TABLE_NAME = 'workspaces' THEN
        ws_id := COALESCE(NEW.id, OLD.id);
    END IF;

    SELECT w.state, w.admin_user_id INTO ws_state, ws_admin
      FROM workspaces w WHERE w.id = ws_id;
    IF NOT FOUND OR ws_state <> 'active' THEN
        RETURN NULL;
    END IF;

    SELECT COUNT(*) INTO admin_cnt
      FROM memberships m
     WHERE m.workspace_id = ws_id AND m.role = 'admin' AND m.status = 'active';

    SELECT m.user_id INTO admin_uid
      FROM memberships m
     WHERE m.workspace_id = ws_id AND m.role = 'admin' AND m.status = 'active'
     LIMIT 1;

    IF admin_cnt <> 1 THEN
        RAISE EXCEPTION
          'У действующего бюджета должен быть ровно один администратор, найдено %', admin_cnt
          USING ERRCODE = 'check_violation';
    END IF;
    IF ws_admin IS DISTINCT FROM admin_uid THEN
        RAISE EXCEPTION 'workspaces.admin_user_id не совпадает с действующим членством admin'
          USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;
"""


def upgrade() -> None:
    op.execute(FIXED_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION check_workspace_admin_exactly_one() FROM PUBLIC")


def downgrade() -> None:
    op.execute(PREVIOUS_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION check_workspace_admin_exactly_one() FROM PUBLIC")
