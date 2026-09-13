"""Служебные функции обслуживания и тип чата входящего события

Revision ID: 0009_maint
Revises: 0008_ready

Фоновые процессы не имеют пользовательского контекста RLS: планировщик не видел
ни одного бюджета, а очистка — ни одного приватного черновика (AUD-01).
Выдавать BYPASSRLS обычным ролям нельзя (SEC-02), поэтому добавлены три узкие
SECURITY DEFINER функции с фиксированным поведением (ADR-06):

* ``maintenance_workspaces`` — перечисление обслуживаемых пространств без
  финансовых данных;
* ``maintenance_expire_drafts`` — истечение и очистка исходного текста
  черновиков по сроку (RET-02, RET-03, RET-08);
* ``purge_workspace_data`` — удаление финансовых данных удалённого бюджета
  после наступления срока (ТЗ §24, AUD-15).

Тип чата сохраняется при приёме, чтобы приватный ответ не уходил в групповой
чат (AUD-12): решение не зависит от чтения защищённого payload.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES

revision: str = "0009_maint"
down_revision: str | None = "0008_ready"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKSPACES = """
CREATE OR REPLACE FUNCTION maintenance_workspaces(p_states TEXT[])
RETURNS TABLE (id UUID, timezone TEXT, state TEXT, quarantined BOOLEAN)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT w.id, w.timezone::TEXT, w.state::TEXT, w.quarantined
      FROM workspaces w
     WHERE w.state = ANY(p_states)
$$;
"""

EXPIRE_DRAFTS = """
CREATE OR REPLACE FUNCTION maintenance_expire_drafts(p_now TIMESTAMPTZ)
RETURNS TABLE (expired BIGINT, cleared BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    expired_count BIGINT := 0;
    cleared_count BIGINT := 0;
BEGIN
    WITH updated AS (
        UPDATE drafts
           SET state = 'expired', version = version + 1
         WHERE expires_at <= p_now
           AND state IN ('received','processing','needs_clarification','ready')
        RETURNING 1
    )
    SELECT count(*) INTO expired_count FROM updated;

    WITH cleaned AS (
        UPDATE drafts
           SET raw_text = NULL, transcript = NULL, version = version + 1
         WHERE delete_raw_after <= p_now
           AND (raw_text IS NOT NULL OR transcript IS NOT NULL)
        RETURNING 1
    )
    SELECT count(*) INTO cleared_count FROM cleaned;

    RETURN QUERY SELECT expired_count, cleared_count;
END;
$$;
"""

# Порядок удаления учитывает внешние ключи: сначала зависимые строки.
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
        'candidates','clarifications','drafts','export_files','budget_invites',
        'admin_transfer_proposals'
    ];
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM workspaces w
         WHERE w.id = p_workspace AND w.state IN ('deleting','deleted')
    ) THEN
        RAISE EXCEPTION 'Очистка доступна только для удалённого бюджета';
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


def upgrade() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute("ALTER TABLE inbound_events ADD COLUMN chat_type VARCHAR(24)")
    op.execute("ALTER TABLE budget_deletion_records ADD COLUMN purged_rows BIGINT")
    op.execute(WORKSPACES)
    op.execute(EXPIRE_DRAFTS)
    op.execute(PURGE)
    for name in (
        "maintenance_workspaces(TEXT[])",
        "maintenance_expire_drafts(TIMESTAMPTZ)",
        "purge_workspace_data(UUID)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {name} TO {roles}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS purge_workspace_data(UUID)")
    op.execute("DROP FUNCTION IF EXISTS maintenance_expire_drafts(TIMESTAMPTZ)")
    op.execute("DROP FUNCTION IF EXISTS maintenance_workspaces(TEXT[])")
    op.execute("ALTER TABLE budget_deletion_records DROP COLUMN IF EXISTS purged_rows")
    op.execute("ALTER TABLE inbound_events DROP COLUMN IF EXISTS chat_type")
