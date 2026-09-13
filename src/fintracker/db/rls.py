"""Карта политик RLS (ADR-06, SEC-02/03).

RLS обеспечивает изоляцию выбранного пространства; право выбрать пространство
и выполнить действие проверяет серверная авторизация. Отсутствие контекста
закрывает доступ: ``current_setting(..., true)`` вернёт NULL, а сравнение с
NULL даёт false.
"""

# ruff: noqa: S608 — имена таблиц берутся из фиксированных констант этого модуля,
# пользовательский ввод в генерацию DDL не попадает.

from __future__ import annotations

from typing import Final

# Таблицы, изолированные по app.workspace_id.
WORKSPACE_SCOPED: Final[tuple[str, ...]] = (
    "account_entries",
    "accounts",
    "admin_transfer_proposals",
    "allocations",
    "analysis_preferences",
    "analysis_runs",
    "analytics_snapshots",
    "beneficiaries",
    "budget_invites",
    "budget_lines",
    "budget_periods",
    "budget_transfers",
    "budget_versions",
    "cash_legs",
    "cash_reservations",
    "categories",
    "category_aliases",
    "category_merge_map",
    "classification_rules",
    "coverage_records",
    "export_files",
    "financial_effects",
    "goal_movements",
    "goals",
    "import_batches",
    "import_rows",
    "imported_aggregate_links",
    "income_plans",
    "income_sources",
    "no_spend_markers",
    "occurrence_settlements",
    "occurrences",
    "opening_adjustments",
    "people",
    "period_policies",
    "receivable_entries",
    "receivables",
    "recommendation_direction_mutes",
    "recommendation_feedback",
    "recommendations",
    "reconciliations",
    "recurring_plan_templates",
    "rollovers",
    "schedule_versions",
    "scheduled_items",
    "source_mappings",
    "tags",
    "threshold_events",
    "transaction_links",
    "transaction_revisions",
    "transaction_tags",
    "transactions",
)

# Личный материал: workspace + владелец (ADR-06: visibility=owner).
OWNER_SCOPED_IN_WORKSPACE: Final[tuple[str, ...]] = ("drafts", "candidates", "clarifications")

# Личные таблицы пользователя (политика «свой пользователь»).
USER_SCOPED: Final[tuple[str, ...]] = (
    "users",
    "user_budget_contexts",
    "budget_setup_drafts",
    "notification_preferences",
)

# Неизменяемые для runtime роли: только SELECT и INSERT (DATA_CONTRACT §2.4).
APPEND_ONLY: Final[tuple[str, ...]] = (
    "account_entries",
    "allocations",
    "audit_events",
    "cash_legs",
    "membership_history",
    "transaction_revisions",
)

# Технические глобальные таблицы маршрутизации и состояний: без RLS,
# без открытого финансового payload (ADR-06).
TECHNICAL_GLOBAL: Final[tuple[str, ...]] = (
    "ai_cost_reservations",
    "ai_quota_counters",
    "audit_events",
    "budget_deletion_records",
    "command_results",
    "consumer_receipts",
    "inbound_events",
    "invite_attempts",
    "jobs",
    "logical_messages",
    "membership_history",
    "message_parts",
    "notification_deliveries",
    "outbox_events",
    "parse_attempts",
    "recipient_day_quotas",
    "security_changes",
)

RUNTIME_ROLES: Final[tuple[str, ...]] = ("fintracker_api", "fintracker_worker")


def workspace_policy_sql(table: str) -> list[str]:
    """Политики изоляции по выбранному пространству."""
    predicate = f"{table}.workspace_id::text = current_setting('app.workspace_id', true)"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_ws_select ON {table} FOR SELECT USING ({predicate})",
        f"CREATE POLICY {table}_ws_insert ON {table} FOR INSERT WITH CHECK ({predicate})",
        f"CREATE POLICY {table}_ws_update ON {table} FOR UPDATE "
        f"USING ({predicate}) WITH CHECK ({predicate})",
        f"CREATE POLICY {table}_ws_delete ON {table} FOR DELETE USING ({predicate})",
    ]


def owner_in_workspace_policy_sql(table: str) -> list[str]:
    """Личный материал внутри бюджета: нужен и бюджет, и владелец.

    Для ``candidates``/``clarifications`` владелец определяется через draft.
    """
    ws = f"{table}.workspace_id::text = current_setting('app.workspace_id', true)"
    if table in {"drafts", "author_replies"}:
        owner = f"{table}.owner_user_id::text = current_setting('app.user_id', true)"
    else:
        owner = (
            "EXISTS (SELECT 1 FROM drafts d WHERE d.id = "
            f"{table}.draft_id AND d.workspace_id = {table}.workspace_id "
            "AND d.owner_user_id::text = current_setting('app.user_id', true))"
        )
    predicate = f"({ws}) AND ({owner})"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_owner_select ON {table} FOR SELECT USING ({predicate})",
        f"CREATE POLICY {table}_owner_insert ON {table} FOR INSERT WITH CHECK ({predicate})",
        f"CREATE POLICY {table}_owner_update ON {table} FOR UPDATE "
        f"USING ({predicate}) WITH CHECK ({predicate})",
        f"CREATE POLICY {table}_owner_delete ON {table} FOR DELETE USING ({predicate})",
    ]


def user_policy_sql(table: str) -> list[str]:
    column = "id" if table == "users" else "user_id"
    if table == "budget_setup_drafts":
        column = "owner_user_id"
    # Имена таблиц и столбцов берутся из фиксированных констант этого модуля,
    # пользовательский ввод в DDL не попадает.
    predicate = f"{table}.{column}::text = current_setting('app.user_id', true)"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_self_select ON {table} FOR SELECT USING ({predicate})",
        f"CREATE POLICY {table}_self_insert ON {table} FOR INSERT WITH CHECK ({predicate})",
        f"CREATE POLICY {table}_self_update ON {table} FOR UPDATE "
        f"USING ({predicate}) WITH CHECK ({predicate})",
        f"CREATE POLICY {table}_self_delete ON {table} FOR DELETE USING ({predicate})",
    ]


def special_policy_sql() -> list[str]:
    """Членства, бюджеты, вложения и payload входящих событий.

    Явное исключение bootstrap: SELECT собственных memberships по app.user_id
    без выбранного workspace; SELECT метаданных workspaces — через собственное
    активное членство. Запись наследует только правило выбранного пространства.
    """
    return [
        # --- memberships -------------------------------------------------
        "ALTER TABLE memberships ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE memberships FORCE ROW LEVEL SECURITY",
        "CREATE POLICY memberships_bootstrap_select ON memberships FOR SELECT USING ("
        "  user_id::text = current_setting('app.user_id', true)"
        "  OR workspace_id::text = current_setting('app.workspace_id', true))",
        "CREATE POLICY memberships_ws_insert ON memberships FOR INSERT WITH CHECK ("
        "  workspace_id::text = current_setting('app.workspace_id', true))",
        "CREATE POLICY memberships_ws_update ON memberships FOR UPDATE USING ("
        "  workspace_id::text = current_setting('app.workspace_id', true)) WITH CHECK ("
        "  workspace_id::text = current_setting('app.workspace_id', true))",
        "CREATE POLICY memberships_ws_delete ON memberships FOR DELETE USING ("
        "  workspace_id::text = current_setting('app.workspace_id', true))",
        # --- workspaces ---------------------------------------------------
        "ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE workspaces FORCE ROW LEVEL SECURITY",
        "CREATE POLICY workspaces_member_select ON workspaces FOR SELECT USING ("
        "  id::text = current_setting('app.workspace_id', true)"
        "  OR EXISTS (SELECT 1 FROM memberships m WHERE m.workspace_id = workspaces.id"
        "    AND m.status = 'active'"
        "    AND m.user_id::text = current_setting('app.user_id', true)))",
        "CREATE POLICY workspaces_ws_insert ON workspaces FOR INSERT WITH CHECK ("
        "  id::text = current_setting('app.workspace_id', true))",
        "CREATE POLICY workspaces_ws_update ON workspaces FOR UPDATE USING ("
        "  id::text = current_setting('app.workspace_id', true)) WITH CHECK ("
        "  id::text = current_setting('app.workspace_id', true))",
        "CREATE POLICY workspaces_ws_delete ON workspaces FOR DELETE USING ("
        "  id::text = current_setting('app.workspace_id', true))",
        # --- attachments: личный материал переводится в workspace при commit -
        "ALTER TABLE attachments ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE attachments FORCE ROW LEVEL SECURITY",
        "CREATE POLICY attachments_access ON attachments FOR SELECT USING ("
        "  (visibility = 'owner' AND owner_user_id::text = current_setting('app.user_id', true))"
        "  OR (visibility = 'workspace'"
        "      AND workspace_id::text = current_setting('app.workspace_id', true)))",
        "CREATE POLICY attachments_insert ON attachments FOR INSERT WITH CHECK ("
        "  owner_user_id::text = current_setting('app.user_id', true))",
        "CREATE POLICY attachments_update ON attachments FOR UPDATE USING ("
        "  owner_user_id::text = current_setting('app.user_id', true)"
        "  OR workspace_id::text = current_setting('app.workspace_id', true)) WITH CHECK ("
        "  owner_user_id::text = current_setting('app.user_id', true)"
        "  OR workspace_id::text = current_setting('app.workspace_id', true))",
        # --- inbound_payloads: защищённое содержимое приёма ----------------
        "ALTER TABLE inbound_payloads ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE inbound_payloads FORCE ROW LEVEL SECURITY",
        "CREATE POLICY inbound_payloads_access ON inbound_payloads FOR SELECT USING ("
        "  owner_user_id::text = current_setting('app.user_id', true)"
        "  OR workspace_id::text = current_setting('app.workspace_id', true))",
        "CREATE POLICY inbound_payloads_insert ON inbound_payloads FOR INSERT WITH CHECK (true)",
        "CREATE POLICY inbound_payloads_update ON inbound_payloads FOR UPDATE USING ("
        "  owner_user_id::text = current_setting('app.user_id', true)"
        "  OR workspace_id::text = current_setting('app.workspace_id', true)) WITH CHECK (true)",
        "CREATE POLICY inbound_payloads_delete ON inbound_payloads FOR DELETE USING (true)",
    ]


def grants_sql() -> list[str]:
    roles = ", ".join(RUNTIME_ROLES)
    statements = [
        f"GRANT USAGE ON SCHEMA public TO {roles}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {roles}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {roles}",
    ]
    for table in APPEND_ONLY:
        # Историческая ревизия и движение счёта не изменяются runtime ролью.
        statements.append(f"REVOKE UPDATE, DELETE ON {table} FROM {roles}")
    # alembic_version меняет только владелец миграций; чтение нужно readiness
    # для проверки совместимости схемы (OPS-02).
    statements.append(f"REVOKE ALL ON alembic_version FROM {roles}")
    statements.append(f"GRANT SELECT ON alembic_version TO {roles}")
    return statements


def all_policy_statements() -> list[str]:
    statements: list[str] = []
    for table in WORKSPACE_SCOPED:
        statements.extend(workspace_policy_sql(table))
    for table in OWNER_SCOPED_IN_WORKSPACE:
        statements.extend(owner_in_workspace_policy_sql(table))
    for table in USER_SCOPED:
        statements.extend(user_policy_sql(table))
    statements.extend(special_policy_sql())
    return statements


def all_rls_tables() -> frozenset[str]:
    return frozenset(
        (
            *WORKSPACE_SCOPED,
            *OWNER_SCOPED_IN_WORKSPACE,
            *USER_SCOPED,
            "memberships",
            "workspaces",
            "attachments",
            "inbound_payloads",
        )
    )
