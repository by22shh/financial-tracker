"""Подготовленный ответ автору хранится изолированно

Revision ID: 0011_reply
Revises: 0010_input

Текст ответа участнику содержит суммы, статьи и даты его бюджета. Таблица
задач — техническая и глобальная: по ADR-06 в ней не должно быть открытого
финансового содержимого, а строки видны без контекста бюджета. Поэтому ответ
сохраняется в отдельной строке, изолированной по бюджету и владельцу, а задача
доставки ссылается только на её идентификатор (R-04).

Срок хранения совпадает с исходным текстом разбора (RET-08): очистка идёт
узкой служебной функцией, потому что у фонового процесса нет пользовательского
контекста RLS.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import RUNTIME_ROLES, owner_in_workspace_policy_sql

revision: str = "0011_reply"
down_revision: str | None = "0010_input"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = """
CREATE TABLE author_replies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    owner_user_id UUID NOT NULL,
    inbound_event_id UUID,
    chat_id BIGINT NOT NULL,
    messages JSONB NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delete_after TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_author_replies_state_allowed
        CHECK (state IN ('pending','sent','cancelled')),
    CONSTRAINT uq_author_replies_workspace_id_id UNIQUE (workspace_id, id)
);

CREATE INDEX ix_author_replies_event ON author_replies (workspace_id, inbound_event_id);
CREATE INDEX ix_author_replies_retention ON author_replies (delete_after);
"""

PURGE_REPLIES = """
CREATE OR REPLACE FUNCTION maintenance_purge_author_replies(p_now TIMESTAMPTZ)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    removed BIGINT := 0;
BEGIN
    WITH deleted AS (
        DELETE FROM author_replies WHERE delete_after <= p_now RETURNING 1
    )
    SELECT count(*) INTO removed FROM deleted;
    RETURN removed;
END;
$$;
"""

# Список таблиц очистки бюджета дополняется новой строкой ответа автору.
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
        'attachments','author_replies','candidates','clarifications','drafts',
        'export_files','budget_invites','admin_transfer_proposals'
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


def upgrade() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(TABLE)
    for statement in owner_in_workspace_policy_sql("author_replies"):
        op.execute(statement)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON author_replies TO {roles}")
    op.execute(PURGE_REPLIES)
    op.execute(PURGE)
    for name in ("maintenance_purge_author_replies(TIMESTAMPTZ)", "purge_workspace_data(UUID)"):
        op.execute(f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {name} TO {roles}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS maintenance_purge_author_replies(TIMESTAMPTZ)")
    op.execute("DROP TABLE IF EXISTS author_replies")
