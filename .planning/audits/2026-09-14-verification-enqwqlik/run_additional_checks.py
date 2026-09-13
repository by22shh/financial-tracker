#!/usr/bin/env python3
"""Run follow-up probes on an isolated snapshot and a unique local PostgreSQL DB."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def capture(repo: Path, snapshot: Path) -> tuple[str, dict[str, str]]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=repo).decode().split("\0")
    paths += subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "src", "tests", ".planning/tools"],
        cwd=repo,
    ).decode().split("\0")
    hashes = {}
    for relative in sorted(set(paths)):
        if not relative or relative.startswith((".planning/audits/", ".research-private/")):
            continue
        source = repo / relative
        if not source.is_file() or (source.name.startswith(".env") and source.name != ".env.example"):
            continue
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    changed = [name for name, digest in hashes.items()
               if not (repo/name).is_file() or hashlib.sha256((repo/name).read_bytes()).hexdigest() != digest]
    if changed or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() != head:
        raise RuntimeError(f"Working tree changed during capture: {changed}")
    return head, hashes


def main() -> int:
    evidence = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=evidence.parents[2])
    parser.add_argument("--pg-port", type=int, default=55432)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not (repo/".venv/bin/python").is_file():
        parser.error("Repository .venv is required")
    snapshot = Path(tempfile.mkdtemp(prefix="fintracker-verification3-"))
    head, hashes = capture(repo, snapshot)
    (snapshot/".venv").symlink_to(repo/".venv", target_is_directory=True)
    shutil.copy2(evidence/"test_verification3_cases.py", snapshot/"tests/integration/test_verification3_cases.py")
    suffix = uuid.uuid4().hex[:12]
    output = evidence / ("run-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + suffix[:4])
    output.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("FINTRACKER_")}
    env.update(
        FINTRACKER_TEST_DB="fintracker_verify3_" + suffix,
        FINTRACKER_TEST_PG_HOST="localhost",
        FINTRACKER_TEST_PG_PORT=str(args.pg_port),
        FINTRACKER_TEST_PG_PASSWORD=os.environ.get("FINTRACKER_TEST_PG_PASSWORD", "devpassword"),
        FINTRACKER_AI__ENABLED="false", FINTRACKER_ASR__PROVIDER="none",
        PYTHONPATH=str(snapshot/"src"),
    )
    (output/"scope.json").write_text(json.dumps({
        "head": head, "snapshot": str(snapshot), "source_hashes": hashes,
        "test_database": env["FINTRACKER_TEST_DB"], "external_calls": False,
    }, indent=2) + "\n")
    print(f"Snapshot: {snapshot}\nEvidence: {output}", flush=True)
    with (output/"pytest.log").open("w") as log:
        result = subprocess.run([
            str(snapshot/".venv/bin/python"), "-m", "pytest",
            "tests/integration/test_verification3_cases.py", "-q", "--tb=short",
            "--junitxml=" + str(output/"junit.xml"),
        ], cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)
    print(f"pytest exit: {result.returncode}; see {output/'pytest.log'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
