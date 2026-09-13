#!/usr/bin/env python3
"""Run audit probes in a git archive with a unique local test database.

Application sources and the working database are not changed. PostgreSQL must
already be available on localhost with the repository's development roles.
The probes describe expected behavior; failures on the audited commit reproduce
the findings. Four downstream probes isolate the inbox RLS defect in test code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import uuid

AUDITED_COMMIT = "57ed972503abccc14238fe5951498e4b0e5ff78f"


def main() -> int:
    evidence = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=evidence.parents[2])
    parser.add_argument("--commit", default=AUDITED_COMMIT)
    parser.add_argument("--pg-port", type=int, default=55432)
    args = parser.parse_args()
    repo = args.repo.resolve()
    python = repo / ".venv/bin/python"
    if not python.exists():
        parser.error(f"Virtual environment not found: {python}")

    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.commit + "^{commit}"],
        cwd=repo, text=True,
    ).strip()
    suffix = uuid.uuid4().hex[:12]
    snapshot = Path(tempfile.mkdtemp(prefix="fintracker-audit-repro-"))
    run_dir = evidence / ("run-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + suffix[:4])
    run_dir.mkdir()
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", commit], cwd=repo,
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(snapshot, filter="data")
    (snapshot / ".venv").symlink_to(repo / ".venv", target_is_directory=True)
    for source, target in (
        ("repros.py", "test_deep_audit.py"),
        ("repros_v2.py", "test_deep_audit_v2.py"),
        ("repros_v3.py", "test_deep_audit_v3.py"),
    ):
        shutil.copy2(evidence / source, snapshot / "tests/integration" / target)

    # Discard application/production overrides and use only a new local test DB.
    env = {key: value for key, value in os.environ.items() if not key.startswith("FINTRACKER_")}
    env.update(
        FINTRACKER_TEST_DB="fintracker_audit_repro_" + suffix,
        FINTRACKER_TEST_PG_HOST="localhost",
        FINTRACKER_TEST_PG_PORT=str(args.pg_port),
        FINTRACKER_TEST_PG_PASSWORD=os.environ.get("FINTRACKER_TEST_PG_PASSWORD", "devpassword"),
        FINTRACKER_AI__ENABLED="false",
        FINTRACKER_ASR__PROVIDER="none",
        PYTHONPATH=str(snapshot / "src"),
    )
    command = [
        str(snapshot / ".venv/bin/python"), "-m", "pytest",
        "tests/integration/test_deep_audit_v2.py",
        "tests/integration/test_deep_audit_v3.py",
        "-q", "--tb=short", "--junitxml=" + str(run_dir / "junit.xml"),
    ]
    metadata = {
        "commit": commit, "snapshot": str(snapshot),
        "test_database": env["FINTRACKER_TEST_DB"],
        "local_pg_port": args.pg_port,
        "expected_on_audited_commit": {"tests": 21, "failures": 21, "passed": 0},
        "note": "No live AI, ASR or Telegram requests; snapshot retained for inspection.",
    }
    (run_dir / "scope.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Snapshot: {snapshot}\nEvidence: {run_dir}", flush=True)
    with (run_dir / "pytest.log").open("w") as output:
        result = subprocess.run(command, cwd=snapshot, env=env, stdout=output, stderr=subprocess.STDOUT)
    print(f"pytest exit code: {result.returncode}; see {run_dir / 'pytest.log'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
