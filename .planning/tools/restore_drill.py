#!/usr/bin/env python3
"""Учение по восстановлению базы (AR-33, NFR-10, RET-09).

Снимает дамп рабочей тестовой базы, восстанавливает его в отдельную базу и
проверяет финансовые инварианты на восстановленной копии.

Что измеряется честно:

* RTO — время от начала учения до проверенной восстановленной копии. Время
  обнаружения аварии и принятия решения в него не входит и указывается явно.
* Окно потери данных этого учения — возраст последней восстановимой записи.
  Метка пишется до дампа и после него: запись, сделанная после снимка, в копии
  отсутствовать обязана, и это подтверждается.

Что **не** измеряется: RPO по NFR-10. Без непрерывного архива WAL точка
восстановления ограничена интервалом резервного копирования, а длительность
дампа к потере данных отношения не имеет. Поле ``rpo_demonstrated`` остаётся
false до настройки PITR (BL-04).

Запуск: .venv/bin/python .planning/tools/restore_drill.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".planning" / "evidence"
CONTAINER = os.environ.get("FINTRACKER_PG_CONTAINER", "fintracker_pg")
SOURCE_DB = os.environ.get("FINTRACKER_TEST_DB", "fintracker_test")
TARGET_DB = os.environ.get("FINTRACKER_RESTORE_DB", "fintracker_restore")
OWNER = "fintracker_owner"

# Требование NFR-10: восстановление RPO <= 1 час, RTO <= 4 часа.
RPO_LIMIT_SECONDS = 3600
RTO_LIMIT_SECONDS = 4 * 3600

# Метка последней восстановимой записи: пишется в исходную базу до и после
# дампа, чтобы возраст точки восстановления измерялся, а не предполагался.
HEARTBEAT_TABLE = "restore_drill_heartbeat"
# Запросы метки собраны здесь как константы: имя таблицы фиксировано в этом
# файле и не строится из пользовательского ввода.
CREATE_HEARTBEAT = (
    f"CREATE TABLE IF NOT EXISTS {HEARTBEAT_TABLE} "
    "(id BIGSERIAL PRIMARY KEY, label TEXT NOT NULL, beat_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)
BEAT_BEFORE = f"INSERT INTO {HEARTBEAT_TABLE}(label) VALUES ('before_dump')"  # noqa: S608
BEAT_AFTER = f"INSERT INTO {HEARTBEAT_TABLE}(label) VALUES ('after_dump')"  # noqa: S608
BEAT_POINT = f"SELECT max(beat_at) FROM {HEARTBEAT_TABLE}"  # noqa: S608
BEAT_LABELS = f"SELECT string_agg(label, ',' ORDER BY id) FROM {HEARTBEAT_TABLE}"  # noqa: S608
DROP_HEARTBEAT = f"DROP TABLE IF EXISTS {HEARTBEAT_TABLE}"

# Фиксированные запросы подсчёта: имя таблицы не строится из ввода.
COUNT_SQL: dict[str, str] = {
    "transactions": "SELECT count(*) FROM transactions",
    "transaction_revisions": "SELECT count(*) FROM transaction_revisions",
    "allocations": "SELECT count(*) FROM allocations",
    "account_entries": "SELECT count(*) FROM account_entries",
}

INVARIANTS: dict[str, str] = {
    "allocations_match_revision_amount": """
        SELECT count(*) FROM (
            SELECT r.workspace_id, r.transaction_id, r.revision, r.amount_minor,
                   COALESCE(SUM(
                       CASE WHEN a.economic_role IN ('expense','interest_expense','income',
                                                     'principal_repayment','goal_allocation',
                                                     'unclassified','external_funding',
                                                     'receivable_increase','receivable_decrease',
                                                     'liability_decrease','expense_refund',
                                                     'receivable_reversal')
                            THEN a.amount_minor ELSE 0 END), 0) AS parts
            FROM transaction_revisions r
            LEFT JOIN allocations a
              ON a.workspace_id = r.workspace_id
             AND a.transaction_id = r.transaction_id
             AND a.revision = r.revision
            GROUP BY r.workspace_id, r.transaction_id, r.revision, r.amount_minor
            HAVING COALESCE(SUM(a.amount_minor), 0) <> r.amount_minor
        ) AS broken
    """,
    "single_active_effect": """
        SELECT count(*) FROM (
            SELECT transaction_id FROM financial_effects
            WHERE is_active
            GROUP BY workspace_id, transaction_id
            HAVING count(*) > 1
        ) AS broken
    """,
    "account_entries_match_effects": """
        SELECT count(*) FROM account_entries e
        LEFT JOIN financial_effects f ON f.id = e.effect_id
        WHERE f.id IS NULL
    """,
    "exactly_one_admin": """
        SELECT count(*) FROM (
            SELECT workspace_id FROM memberships
            WHERE role = 'admin' AND status = 'active'
            GROUP BY workspace_id
            HAVING count(*) <> 1
        ) AS broken
    """,
}


def docker(*args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    # Имена таблиц и путь к docker берутся из фиксированного списка этого файла,
    # пользовательский ввод в команду не попадает.
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607 - docker берётся из PATH среды разработчика
        input=input_bytes,
        capture_output=True,
        check=False,
    )


def psql(database: str, sql: str) -> str:
    result = docker(
        "exec",
        "-i",
        CONTAINER,
        "psql",
        "-U",
        OWNER,
        "-d",
        database,
        "-tAc",
        sql,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-2000:])
    return result.stdout.decode("utf-8", "replace").strip()


def main() -> int:
    if docker("inspect", CONTAINER).returncode != 0:
        print(f"Контейнер {CONTAINER} недоступен: поднимите среду `make up`.")
        return 2
    exists = psql(
        "postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{SOURCE_DB}'"  # noqa: S608
    )
    if int(exists) == 0:
        # База проверок создаётся на время прогона: учение запускается вместе
        # с ним (`FINTRACKER_PERF=1 pytest -m slow`), а не после.
        print(f"База {SOURCE_DB} отсутствует: запустите учение вместе с прогоном тестов.")
        return 2

    started = time.perf_counter()
    psql(SOURCE_DB, CREATE_HEARTBEAT)
    psql(SOURCE_DB, BEAT_BEFORE)
    recovery_point = psql(SOURCE_DB, BEAT_POINT)
    dump = docker("exec", CONTAINER, "pg_dump", "-U", OWNER, "-Fc", SOURCE_DB)
    if dump.returncode != 0:
        print(dump.stderr.decode("utf-8", "replace")[-2000:])
        return 1
    dump_seconds = time.perf_counter() - started
    dump_bytes = len(dump.stdout)
    # Запись после снимка заведомо не должна попасть в копию.
    psql(SOURCE_DB, BEAT_AFTER)

    psql("postgres", f'DROP DATABASE IF EXISTS "{TARGET_DB}"')
    psql("postgres", f'CREATE DATABASE "{TARGET_DB}" OWNER {OWNER}')

    restore_started = time.perf_counter()
    restore = docker(
        "exec",
        "-i",
        CONTAINER,
        "pg_restore",
        "-U",
        OWNER,
        "-d",
        TARGET_DB,
        "--no-owner",
        "--exit-on-error",
        input_bytes=dump.stdout,
    )
    restore_seconds = time.perf_counter() - restore_started
    if restore.returncode != 0:
        # Частичное восстановление не считается успешным: непрочитанная часть
        # дампа означает потерю данных (AR-33).
        print(restore.stderr.decode("utf-8", "replace")[-2000:])
        psql("postgres", f'DROP DATABASE IF EXISTS "{TARGET_DB}"')
        return 1

    failures: dict[str, int] = {}
    for name, sql in INVARIANTS.items():
        value = int(psql(TARGET_DB, " ".join(sql.split())))
        if value:
            failures[name] = value

    tables = ("transactions", "transaction_revisions", "allocations", "account_entries")
    counts = {table: int(psql(TARGET_DB, COUNT_SQL[table])) for table in tables}
    source_counts = {table: int(psql(SOURCE_DB, COUNT_SQL[table])) for table in tables}

    restored_labels = psql(TARGET_DB, BEAT_LABELS)
    restored_point = psql(TARGET_DB, BEAT_POINT)
    # Копия обязана содержать запись до снимка и не содержать сделанную после.
    snapshot_is_consistent = restored_labels == "before_dump"

    total_seconds = time.perf_counter() - started
    payload = {
        "measured_at": dt.datetime.now(dt.UTC).isoformat(),
        "source_database": SOURCE_DB,
        "restored_database": TARGET_DB,
        "dump_seconds": round(dump_seconds, 2),
        "dump_bytes": dump_bytes,
        "restore_seconds": round(restore_seconds, 2),
        "total_seconds": round(total_seconds, 2),
        "recovery_point": recovery_point,
        "restored_recovery_point": restored_point,
        "restored_heartbeat_labels": restored_labels,
        "snapshot_is_consistent": snapshot_is_consistent,
        "recovery_point_age_seconds": round(total_seconds, 2),
        "rpo_demonstrated": False,
        "rpo_limit_seconds": RPO_LIMIT_SECONDS,
        "rpo_method": (
            "not_measured: без непрерывного архива WAL точка восстановления "
            "ограничена интервалом резервного копирования; длительность дампа "
            "потерю данных не измеряет (BL-04)"
        ),
        "rto_seconds_measured": round(total_seconds, 2),
        "rto_limit_seconds": RTO_LIMIT_SECONDS,
        "rto_method": (
            "измерено: дамп, восстановление и проверка инвариантов; время "
            "обнаружения аварии и принятия решения не включено"
        ),
        "invariant_failures": failures,
        "restored_counts": counts,
        "source_counts": source_counts,
        "row_counts_match": counts == source_counts,
        "risk_tail": (
            "Учение выполнено на локальном снимке без WAL архива: непрерывный "
            "PITR и офсайт-копия требуют выбранного хостинга (BL-04)."
        ),
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "restore_drill.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    psql("postgres", f'DROP DATABASE IF EXISTS "{TARGET_DB}"')
    psql(SOURCE_DB, DROP_HEARTBEAT)
    if failures or not payload["row_counts_match"]:
        return 1
    if not snapshot_is_consistent:
        return 1
    if payload["rto_seconds_measured"] > RTO_LIMIT_SECONDS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
