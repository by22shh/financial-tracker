#!/usr/bin/env python3
"""Сбор доказательств проверок: команда, дата, версия кода и схемы, результат."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".planning" / "evidence"


def run(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, cwd=ROOT, check=False
    )
    return result.returncode, (result.stdout + result.stderr)


def git_revision() -> str:
    code, out = run(["git", "rev-parse", "--short", "HEAD"])
    return out.strip() if code == 0 else "uncommitted"


def working_tree_fingerprint() -> str:
    """Отпечаток незакоммиченного состояния (раздел 9 инструкции)."""
    code, out = run(["git", "status", "--porcelain"])
    if code != 0:
        return "unknown"
    import hashlib

    return hashlib.sha256(out.encode()).hexdigest()[:16]


def schema_revision() -> str:
    code, out = run([".venv/bin/alembic", "heads"])
    return out.strip().splitlines()[0] if code == 0 and out.strip() else "unknown"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now(dt.UTC)
    checks = {
        "format": [".venv/bin/ruff", "format", "--check", "src", "tests"],
        "lint": [".venv/bin/ruff", "check", "src", "tests"],
        "types": [".venv/bin/mypy", "src/fintracker"],
        "tests": [".venv/bin/pytest", "-q", "--tb=short"],
    }
    report: dict[str, object] = {
        "started_at": started.isoformat(),
        "git_revision": git_revision(),
        "working_tree_fingerprint": working_tree_fingerprint(),
        "schema_revision": schema_revision(),
        "python": sys.version.split()[0],
        "checks": {},
    }
    failures = 0
    for name, command in checks.items():
        code, output = run(command)
        tail = "\n".join(output.splitlines()[-40:])
        report["checks"][name] = {  # type: ignore[index]
            "command": " ".join(command),
            "exit_code": code,
            "result": "PASS" if code == 0 else "FAIL",
            "output_tail": tail,
        }
        failures += int(code != 0)
    report["finished_at"] = dt.datetime.now(dt.UTC).isoformat()
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
