#!/usr/bin/env python3
"""Сбор доказательств проверок: команда, дата, версия кода и схемы, результат.

Прогон тестов сохраняется в JUnit XML с результатом каждого узла: статус
``verified`` в реестре требований привязывается к конкретному commit, к
фактически собранному pytest-узлу и к его исходу, а не к наличию ссылки (R-12).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

from evidence_source import capture_source, file_digest

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".planning" / "evidence"
# Имя относительно корня проекта: pytest запускается с cwd=ROOT.
JUNIT_NAME = ".planning/evidence/latest-junit.xml"


def run(command: list[str], extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    environment = {**os.environ, **(extra_env or {})}
    result = subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, cwd=ROOT, check=False, env=environment
    )
    return result.returncode, (result.stdout + result.stderr)


def git_revision() -> str:
    code, out = run(["git", "rev-parse", "HEAD"])
    return out.strip() if code == 0 else "uncommitted"


def schema_revision() -> str:
    code, out = run([".venv/bin/alembic", "heads"])
    return out.strip().splitlines()[0] if code == 0 and out.strip() else "unknown"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now(dt.UTC)
    source_before = capture_source(ROOT)
    junit = ROOT / JUNIT_NAME
    # A failed invocation must never reuse the preceding run's XML.
    junit.unlink(missing_ok=True)
    checks = {
        "format": [".venv/bin/ruff", "format", "--check", "src", "tests"],
        "lint": [".venv/bin/ruff", "check", "src", "tests"],
        "types": [".venv/bin/mypy", "src/fintracker"],
        "tests": [
            ".venv/bin/pytest",
            "-q",
            "--tb=short",
            f"--junitxml={JUNIT_NAME}",
        ],
    }
    report: dict[str, object] = {
        "started_at": started.isoformat(),
        "git_revision": git_revision(),
        "working_tree_fingerprint": source_before["sha256"],
        "source_before": source_before,
        "schema_revision": schema_revision(),
        "python": sys.version.split()[0],
        "checks": {},
    }
    # Измерения запускаются вместе с остальными: статус verified привязан к
    # фактическому исходу узла, а пропуск исходом passed не является (R-12).
    environments = {"tests": {"FINTRACKER_PERF": "1"}}
    failures = 0
    for name, command in checks.items():
        code, output = run(command, environments.get(name))
        tail = "\n".join(output.splitlines()[-40:])
        report["checks"][name] = {  # type: ignore[index]
            "command": " ".join(command),
            "exit_code": code,
            "result": "PASS" if code == 0 else "FAIL",
            "output_tail": tail,
        }
        failures += int(code != 0)
    stamped_junit = EVIDENCE / f"junit-{started.strftime('%Y%m%d-%H%M%S')}.xml"
    if junit.exists():
        stamped_junit.write_bytes(junit.read_bytes())
    report["test_report"] = {
        "path": JUNIT_NAME,
        "archived": str(stamped_junit.relative_to(ROOT)) if junit.exists() else None,
        "sha256": file_digest(junit) if junit.exists() else None,
    }
    report["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
    report["source_after"] = capture_source(ROOT)
    if source_before != report["source_after"]:
        failures += 1
        report["source_error"] = "Sources changed while checks were running"
    if not junit.exists():
        failures += 1
    report["overall"] = "PASS" if failures == 0 else "FAIL"

    stamp = started.strftime("%Y%m%d-%H%M%S")
    path = EVIDENCE / f"checks-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (EVIDENCE / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"{report['overall']}: {path.relative_to(ROOT)}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
