#!/usr/bin/env python3
"""Repeat follow-up audit probes against the current working tree in isolation."""
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


def main() -> int:
    evidence = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=evidence.parents[2])
    parser.add_argument("--pg-port", type=int, default=55432)
    args = parser.parse_args()
    root = args.repo.resolve()
    if not (root / ".venv/bin/python").exists():
        parser.error("The repository virtual environment .venv is required.")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "src", "tests", ".planning/tools"],
        cwd=root,
    ).decode().split("\0")
    snapshot = Path(tempfile.mkdtemp(prefix="fintracker-recheck-"))
    hashes = {}
    for relative in sorted(set(tracked + untracked)):
        if not relative or relative.startswith((".planning/audits/", ".research-private/")):
            continue
        source = root / relative
        if not source.is_file() or source.name.startswith(".env"):
            continue
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if relative.startswith(("src/", "tests/", ".planning/tools/")) or relative in ("pyproject.toml", "uv.lock", "Makefile"):
            hashes[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()

    # Detect edits during capture instead of testing a mixed source snapshot.
    changed = [relative for relative, digest in hashes.items()
               if not (root / relative).is_file()
               or hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest]
    current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if changed or current_head != head:
        parser.error(f"Working tree changed during capture; repeat capture. Files: {changed}")
    (snapshot / ".venv").symlink_to(root / ".venv", target_is_directory=True)
    for source, target in (
        ("legacy_support.py", "_recheck_legacy_support.py"),
        ("test_recheck_prior_v2.py", "test_recheck_prior_v2.py"),
        ("test_recheck_prior_v3.py", "test_recheck_prior_v3.py"),
        ("extra_repros.py", "test_recheck_adversarial.py"),
    ):
        shutil.copy2(evidence / source, snapshot / "tests/integration" / target)

    suffix = uuid.uuid4().hex[:12]
    output = evidence / ("run-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + suffix[:4])
    output.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("FINTRACKER_")}
    env.update(
        FINTRACKER_TEST_DB="fintracker_recheck_" + suffix,
        FINTRACKER_TEST_PG_HOST="localhost",
        FINTRACKER_TEST_PG_PORT=str(args.pg_port),
        FINTRACKER_TEST_PG_PASSWORD=os.environ.get("FINTRACKER_TEST_PG_PASSWORD", "devpassword"),
        FINTRACKER_AI__ENABLED="false",
        FINTRACKER_ASR__PROVIDER="none",
        PYTHONPATH=str(snapshot / "src"),
    )
    (output / "scope.json").write_text(json.dumps({
        "head": head, "snapshot": str(snapshot), "source_hashes": hashes,
        "test_database": env["FINTRACKER_TEST_DB"],
        "note": "Current tracked changes and untracked source files included. No live external calls.",
    }, indent=2) + "\n")
    print(f"Snapshot: {snapshot}\nEvidence: {output}", flush=True)
    with (output / "pytest.log").open("w") as log:
        result = subprocess.run(
            [str(snapshot / ".venv/bin/python"), "-m", "pytest",
             "tests/integration/test_recheck_prior_v2.py",
             "tests/integration/test_recheck_prior_v3.py",
             "tests/integration/test_recheck_adversarial.py",
             "-q", "--tb=short", "--junitxml=" + str(output / "junit.xml")],
            cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    print(f"pytest exit code: {result.returncode}; see {output / 'pytest.log'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

