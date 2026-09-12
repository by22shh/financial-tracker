"""RLS, права runtime ролей и триггеры денежных инвариантов

Revision ID: 0002_rls
Revises: b41bf8f75ab2
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from fintracker.db.rls import all_policy_statements, all_rls_tables, grants_sql

revision: str = "0002_rls"
down_revision: str | None = "b41bf8f75ab2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Триггеры денежных инвариантов (DATA_CONTRACT §2.4) ---------------------
# CHECK с подзапросом SUM не используется: PostgreSQL не гарантирует его
# корректность при изменении других строк. Поэтому применяются DEFERRABLE
# INITIALLY DEFERRED constraint triggers, проверяющие итог к концу commit.

INVARIANT_FUNCTION = """
CREATE OR REPLACE FUNCTION check_revision_money_invariants() RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    rev            RECORD;
    alloc_total    BIGINT;
    cash_total     BIGINT;
    cash_abs_total BIGINT;
    expense_roles  TEXT[] := ARRAY['expense'];
    ws             UUID;
    txn            UUID;
    revno          BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RETURN NULL;
    END IF;

    IF TG_TABLE_NAME = 'transaction_revisions' THEN
        ws := NEW.workspace_id; txn := NEW.transaction_id; revno := NEW.revision;
    ELSE
        ws := NEW.workspace_id; txn := NEW.transaction_id; revno := NEW.revision;
    END IF;

    SELECT * INTO rev FROM transaction_revisions r
     WHERE r.workspace_id = ws AND r.transaction_id = txn AND r.revision = revno;
    IF NOT FOUND THEN
        -- Ревизия удалена в этой же транзакции: проверять нечего.
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(a.amount_minor), 0) INTO alloc_total
      FROM allocations a
     WHERE a.workspace_id = ws AND a.transaction_id = txn AND a.revision = revno;

    SELECT COALESCE(SUM(c.signed_minor), 0), COALESCE(SUM(ABS(c.signed_minor)), 0)
      INTO cash_total, cash_abs_total
      FROM cash_legs c
     WHERE c.workspace_id = ws AND c.transaction_id = txn AND c.revision = revno;

    IF rev.transaction_type IN ('expense', 'mixed_payment') THEN
        IF alloc_total <> rev.amount_minor THEN
            RAISE EXCEPTION
              'Сумма распределений % не равна сумме операции % (%, ревизия %)',
              alloc_total, rev.amount_minor, rev.transaction_type, revno
              USING ERRCODE = 'check_violation';
        END IF;
        IF cash_total <> -rev.amount_minor THEN
            RAISE EXCEPTION
              'Сумма CashLeg % должна равняться -% для %',
              cash_total, rev.amount_minor, rev.transaction_type
              USING ERRCODE = 'check_violation';
        END IF;

    ELSIF rev.transaction_type IN ('income', 'external_funding', 'loan_received',
                                   'receivable_settlement', 'refund') THEN
        IF cash_total <> rev.amount_minor THEN
            RAISE EXCEPTION
              'Сумма CashLeg % должна равняться +% для %',
              cash_total, rev.amount_minor, rev.transaction_type
              USING ERRCODE = 'check_violation';
        END IF;
        IF rev.transaction_type = 'refund' AND alloc_total <> rev.amount_minor THEN
            RAISE EXCEPTION
              'Сумма возвратных частей % не равна сумме возврата %',
              alloc_total, rev.amount_minor
              USING ERRCODE = 'check_violation';
        END IF;
        IF rev.transaction_type IN ('income', 'external_funding')
           AND EXISTS (SELECT 1 FROM allocations a
                        WHERE a.workspace_id = ws AND a.transaction_id = txn
                          AND a.revision = revno
                          AND a.economic_role = ANY(expense_roles)) THEN
            RAISE EXCEPTION 'У дохода не может быть расходных распределений'
              USING ERRCODE = 'check_violation';
        END IF;

    ELSIF rev.transaction_type = 'transfer' THEN
        IF cash_total <> 0 THEN
            RAISE EXCEPTION 'Перевод должен давать нулевую сумму CashLeg, получено %', cash_total
              USING ERRCODE = 'check_violation';
        END IF;
        IF cash_abs_total <> 2 * rev.amount_minor THEN
            RAISE EXCEPTION 'У перевода должны быть две стороны по %', rev.amount_minor
              USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM allocations a
                    WHERE a.workspace_id = ws AND a.transaction_id = txn
                      AND a.revision = revno AND a.economic_role = ANY(expense_roles)) THEN
            RAISE EXCEPTION 'У перевода не бывает потребительского расхода'
              USING ERRCODE = 'check_violation';
        END IF;

    ELSIF rev.transaction_type = 'loan_principal_payment' THEN
        IF cash_total <> -rev.amount_minor THEN
            RAISE EXCEPTION 'Погашение долга должно уменьшать деньги на %', rev.amount_minor
              USING ERRCODE = 'check_violation';
        END IF;

    ELSIF rev.transaction_type = 'adjustment' THEN
        IF EXISTS (SELECT 1 FROM allocations a
                    WHERE a.workspace_id = ws AND a.transaction_id = txn
                      AND a.revision = revno
                      AND a.economic_role IN ('expense', 'income')) THEN
            RAISE EXCEPTION 'Корректировка не создаёт потребления или заработка'
              USING ERRCODE = 'check_violation';
        END IF;

    ELSIF rev.transaction_type = 'legacy_unclassified_flow' THEN
        IF alloc_total <> rev.amount_minor THEN
            RAISE EXCEPTION
              'Историческое движение должно сохранять исходную сумму: % против %',
              alloc_total, rev.amount_minor
              USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NULL;
END;
$$;
"""

CURRENT_REVISION_FUNCTION = """
CREATE OR REPLACE FUNCTION check_transaction_current_revision() RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM transaction_revisions r
         WHERE r.workspace_id = NEW.workspace_id
           AND r.transaction_id = NEW.id
           AND r.revision = NEW.current_revision
    ) THEN
        RAISE EXCEPTION 'current_revision % не существует у операции %',
          NEW.current_revision, NEW.id USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NULL;
END;
$$;
"""

ADMIN_EXACTLY_ONE_FUNCTION = """
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

    -- MIN(uuid) в PostgreSQL не определена: считаем количество и берём
    -- единственного администратора отдельным запросом.
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

REFUND_LIMIT_FUNCTION = """
CREATE OR REPLACE FUNCTION check_refund_within_returnable() RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    link         RECORD;
    source_amt   BIGINT;
    refunded_amt BIGINT;
BEGIN
    SELECT * INTO link FROM transaction_links l WHERE l.id = NEW.id;
    IF NOT FOUND OR link.link_type <> 'refund_of' OR link.status <> 'active' THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(SUM(a.amount_minor), 0) INTO source_amt
      FROM allocations a
      JOIN transactions t
        ON t.workspace_id = a.workspace_id AND t.id = a.transaction_id
       AND t.current_revision = a.revision
     WHERE a.workspace_id = link.workspace_id
       AND a.transaction_id = link.source_transaction_id
       AND (link.source_stable_line_id IS NULL
            OR a.stable_line_id = link.source_stable_line_id)
       AND t.status = 'posted';

    SELECT COALESCE(SUM(l2.amount_minor), 0) INTO refunded_amt
      FROM transaction_links l2
     WHERE l2.workspace_id = link.workspace_id
       AND l2.link_type = 'refund_of'
       AND l2.status = 'active'
       AND l2.source_transaction_id = link.source_transaction_id
       AND (link.source_stable_line_id IS NULL
            OR l2.source_stable_line_id = link.source_stable_line_id);

    IF refunded_amt > source_amt THEN
        RAISE EXCEPTION
          'Возвраты % превышают возвращаемую сумму % по исходной покупке',
          refunded_amt, source_amt USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;
"""

TRIGGERS = [
    # Суммы частей и движений проверяются к концу commit.
    "CREATE CONSTRAINT TRIGGER trg_revision_money_invariants "
    "AFTER INSERT OR UPDATE ON transaction_revisions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION check_revision_money_invariants()",
    "CREATE CONSTRAINT TRIGGER trg_allocation_money_invariants "
    "AFTER INSERT OR UPDATE ON allocations "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION check_revision_money_invariants()",
    "CREATE CONSTRAINT TRIGGER trg_cash_leg_money_invariants "
    "AFTER INSERT OR UPDATE ON cash_legs "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION check_revision_money_invariants()",
    "CREATE CONSTRAINT TRIGGER trg_transaction_current_revision "
    "AFTER INSERT OR UPDATE OF current_revision ON transactions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION check_transaction_current_revision()",
    "CREATE CONSTRAINT TRIGGER trg_membership_admin_exactly_one "
    "AFTER INSERT OR UPDATE OR DELETE ON memberships "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION check_workspace_admin_exactly_one()",
    "CREATE CONSTRAINT TRIGGER trg_workspace_admin_exactly_one "
    "AFTER UPDATE OF state, admin_user_id ON workspaces "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION check_workspace_admin_exactly_one()",
    "CREATE CONSTRAINT TRIGGER trg_refund_within_returnable "
    "AFTER INSERT OR UPDATE ON transaction_links "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION check_refund_within_returnable()",
]


def upgrade() -> None:
    for statement in all_policy_statements():
        op.execute(statement)
    for statement in grants_sql():
        op.execute(statement)
    for function in (
        INVARIANT_FUNCTION,
        CURRENT_REVISION_FUNCTION,
        ADMIN_EXACTLY_ONE_FUNCTION,
        REFUND_LIMIT_FUNCTION,
    ):
        op.execute(function)
    for trigger in TRIGGERS:
        op.execute(trigger)
    # Функции проверки не должны быть исполнимы посторонними (ADR-06).
    for func in (
        "check_revision_money_invariants()",
        "check_transaction_current_revision()",
        "check_workspace_admin_exactly_one()",
        "check_refund_within_returnable()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {func} FROM PUBLIC")


def downgrade() -> None:
    for trigger, table in (
        ("trg_revision_money_invariants", "transaction_revisions"),
        ("trg_allocation_money_invariants", "allocations"),
        ("trg_cash_leg_money_invariants", "cash_legs"),
        ("trg_transaction_current_revision", "transactions"),
        ("trg_membership_admin_exactly_one", "memberships"),
        ("trg_workspace_admin_exactly_one", "workspaces"),
        ("trg_refund_within_returnable", "transaction_links"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for func in (
        "check_revision_money_invariants",
        "check_transaction_current_revision",
        "check_workspace_admin_exactly_one",
        "check_refund_within_returnable",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {func}()")
    for table in sorted(all_rls_tables()):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
