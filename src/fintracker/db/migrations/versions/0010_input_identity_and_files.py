"""Идентичность пользовательского ввода и обслуживание файлов

Revision ID: 0010_input
Revises: 0009_maint

Одно сообщение участника — один результат. Уникальность случайного UUID
кандидата не заменяет уникальность действия пользователя: два параллельных
исполнения одного входа успевали не увидеть черновик друг друга и создавали
две траты (R-01). Постоянный ключ исходного сообщения делает получение
черновика атомарным, а редакция того же сообщения адресует прежний ввод (R-03).

Фоновая очистка файлов выполняется узкими SECURITY DEFINER функциями: обычная
WORKER-роль не видит защищённые строки вложений и не должна получать BYPASSRLS
(SEC-02, ADR-06, R-09). Удаление бюджета очищает и его файлы.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES

revision: str = "0010_input"
down_revision: str | None = "0009_maint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ключ исходного сообщения постоянен для всех редакций одного сообщения.
DRAFT_IDENTITY = """
ALTER TABLE drafts ADD COLUMN source_message_key VARCHAR(120);

CREATE UNIQUE INDEX uq_drafts_source_message
    ON drafts (workspace_id, owner_user_id, source_message_key)
 WHERE source_message_key IS NOT NULL AND state <> 'cancelled';
"""

DUE_ATTACHMENTS = """
CREATE OR REPLACE FUNCTION maintenance_due_attachments(
    p_now TIMESTAMPTZ, p_staging_cutoff TIMESTAMPTZ, p_limit INT
)
RETURNS TABLE (id UUID, storage_key TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    RETURN QUERY
    WITH due AS (
        SELECT a.id
          FROM attachments a
         WHERE a.state = 'deleting'
            OR (a.state = 'ready' AND a.delete_after <= p_now)
            OR (a.state = 'staging' AND a.created_at <= p_staging_cutoff)
         ORDER BY a.delete_after
         LIMIT p_limit
    ), marked AS (
        UPDATE attachments a
           SET state = 'deleting'
          FROM due
         WHERE a.id = due.id
     RETURNING a.id, a.storage_key
    )
    SELECT marked.id, marked.storage_key::TEXT FROM marked;
END;
$$;
"""

# Строка вложения остаётся как запись об удалении: объект уже недоступен.
FINISH_ATTACHMENT = """
CREATE OR REPLACE FUNCTION maintenance_finish_attachment(p_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE attachments SET state = 'deleted' WHERE id = p_id AND state = 'deleting'
    RETURNING true
$$;
"""

DUE_EXPORTS = """
CREATE OR REPLACE FUNCTION maintenance_due_exports(p_now TIMESTAMPTZ, p_limit INT)
RETURNS TABLE (id UUID, storage_key TEXT)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT e.id, e.storage_key::TEXT
      FROM export_files e
     WHERE e.delete_after <= p_now
       AND e.state = 'ready'
       AND e.storage_key IS NOT NULL
     ORDER BY e.delete_after
     LIMIT p_limit
$$;
"""

FINISH_EXPORT = """
CREATE OR REPLACE FUNCTION maintenance_finish_export(p_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE export_files SET state = 'deleted', storage_key = NULL
     WHERE id = p_id AND state = 'ready'
    RETURNING true
$$;
"""

# Файлы удаляемого бюджета перечисляются до удаления строк: иначе ключи
# хранилища теряются и объекты остаются навсегда.
WORKSPACE_FILES = """
CREATE OR REPLACE FUNCTION maintenance_workspace_files(p_workspace UUID)
RETURNS TABLE (id UUID, storage_key TEXT, file_kind TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM workspaces w
         WHERE w.id = p_workspace AND w.state IN ('deleting','deleted')
    ) THEN
        RAISE EXCEPTION 'Перечисление файлов доступно только для удалённого бюджета';
    END IF;

    RETURN QUERY
    WITH marked AS (
        UPDATE attachments a
           SET state = 'deleting'
         WHERE a.workspace_id = p_workspace AND a.state <> 'deleted'
     RETURNING a.id, a.storage_key
    )
    SELECT marked.id, marked.storage_key::TEXT, 'attachment'::TEXT FROM marked
    UNION ALL
    SELECT e.id, e.storage_key::TEXT, 'export'::TEXT
      FROM export_files e
     WHERE e.workspace_id = p_workspace AND e.storage_key IS NOT NULL;
END;
$$;
"""

PURGE = """
CREATE OR REPLACE FUNCTION purge_workspace_data(p_workspace UUID)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    removed BIGINT := 0;
    affected BIGINT;
    target TEXT;
    ordered TEXT[] := ARRAY[
        'occurrence_settlements','occurrences','schedule_versions','scheduled_items',
        'goal_movements','cash_reservations','goals','receivable_entries','receivables',
        'threshold_events','recommendation_feedback','recommendation_direction_mutes',
        'recommendations','analysis_runs','analytics_snapshots','analysis_preferences',
        'imported_aggregate_links','import_rows','import_batches','source_mappings',
        'coverage_records','reconciliations','opening_adjustments','no_spend_markers',
        'account_entries','financial_effects','transaction_links','transaction_tags',
        'allocations','cash_legs','transaction_revisions','transactions',
        'rollovers','budget_transfers','budget_lines','budget_versions',
        'income_sources','income_plans','recurring_plan_templates','budget_periods',
        'period_policies','classification_rules','category_merge_map','category_aliases',
        'categories','accounts','tags','beneficiaries','people',
        'attachments','candidates','clarifications','drafts','export_files',
        'budget_invites','admin_transfer_proposals'
    ];
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM workspaces w
         WHERE w.id = p_workspace AND w.state IN ('deleting','deleted')
    ) THEN
        RAISE EXCEPTION 'Очистка доступна только для удалённого бюджета';
    END IF;

    IF EXISTS (
        SELECT 1 FROM attachments a
         WHERE a.workspace_id = p_workspace AND a.state NOT IN ('deleted')
    ) THEN
        RAISE EXCEPTION 'Сначала должны быть удалены файлы бюджета';
    END IF;

    FOREACH target IN ARRAY ordered LOOP
        EXECUTE format('DELETE FROM %I WHERE workspace_id = $1', target) USING p_workspace;
        GET DIAGNOSTICS affected = ROW_COUNT;
        removed := removed + affected;
    END LOOP;

    RETURN removed;
END;
$$;
"""

FUNCTIONS = (
    "maintenance_due_attachments(TIMESTAMPTZ, TIMESTAMPTZ, INT)",
    "maintenance_finish_attachment(UUID)",
    "maintenance_due_exports(TIMESTAMPTZ, INT)",
    "maintenance_finish_export(UUID)",
    "maintenance_workspace_files(UUID)",
    "purge_workspace_data(UUID)",
)


def upgrade() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(DRAFT_IDENTITY)
    op.execute(DUE_ATTACHMENTS)
    op.execute(FINISH_ATTACHMENT)
    op.execute(DUE_EXPORTS)
    op.execute(FINISH_EXPORT)
    op.execute(WORKSPACE_FILES)
    op.execute(PURGE)
    for name in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {name} TO {roles}")


def downgrade() -> None:
    previous = import_module("fintracker.db.migrations.versions.0009_runtime_maintenance")
    op.execute(previous.PURGE)
    op.execute("DROP FUNCTION IF EXISTS maintenance_workspace_files(UUID)")
    op.execute("DROP FUNCTION IF EXISTS maintenance_finish_export(UUID)")
    op.execute("DROP FUNCTION IF EXISTS maintenance_due_exports(TIMESTAMPTZ, INT)")
    op.execute("DROP FUNCTION IF EXISTS maintenance_finish_attachment(UUID)")
    op.execute("DROP FUNCTION IF EXISTS maintenance_due_attachments(TIMESTAMPTZ, TIMESTAMPTZ, INT)")
    op.execute("DROP INDEX IF EXISTS uq_drafts_source_message")
    op.execute("ALTER TABLE drafts DROP COLUMN IF EXISTS source_message_key")
