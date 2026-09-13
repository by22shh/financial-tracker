#!/usr/bin/env python3
"""Probe traceability validation in a disposable copy; never edit working evidence."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from run_additional_checks import capture


def main() -> int:
    evidence = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=evidence.parents[2])
    args = parser.parse_args()
    repo = args.repo.resolve()
    snapshot = Path(tempfile.mkdtemp(prefix="fintracker-traceability3-"))
    head, _ = capture(repo, snapshot)
    # Reproduce the complete content manifest, including ignored local files.
    # Git-only capture omitted Finder metadata that the manifest had hashed.
    baseline = json.loads((repo/".planning/evidence/latest.json").read_text())
    for relative in baseline.get("source_before", {}).get("files", {}):
        source, target = repo/relative, snapshot/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # Git is read-only here; commit comparison uses the actual repository.
    env = {**os.environ, "GIT_DIR":str(repo/".git"), "GIT_WORK_TREE":str(snapshot)}
    command = [str(repo/".venv/bin/python"), str(snapshot/".planning/tools/check_traceability.py")]

    def check(label: str) -> dict:
        result = subprocess.run(command, cwd=snapshot, env=env, capture_output=True, text=True)
        return {"scenario":label, "exit_code":result.returncode, "stdout":result.stdout, "stderr":result.stderr}

    results = [check("unaltered_current_evidence")]
    if results[0]["exit_code"]:
        print("Current evidence already fails. Refresh the baseline before isolating these probes.")
        print(results[0]["stdout"])
        return 2
    registry = snapshot/".planning/requirements.yaml"
    original = registry.read_text()
    start = original.index("- id: A01\n")
    end = original.find("\n- id:", start + 1)
    block = original[start:end]
    block = re.sub(r"(  verification:\n)(?:    - .*\n)+",
                   r"\1    - tests/integration/test_recheck_regressions.py::test_nonexistent_audit_node\n", block)
    block = re.sub(r"(  evidence:\n)(?:    - .*\n)+",
                   r"\1    - .planning/evidence/nonexistent-audit-evidence.json\n", block)
    registry.write_text(original[:start] + block + original[end:])
    results.append(check("nonexistent_test_and_evidence"))
    registry.write_text(original)
    latest = snapshot/".planning/evidence/latest.json"
    metadata = json.loads(latest.read_text())
    junit = snapshot/metadata["test_report"]["path"]
    saved = junit.read_bytes()
    junit.write_text('<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0"/></testsuites>')
    results.append(check("zero_collected_nodes_with_stale_metadata"))
    junit.write_bytes(saved)
    metadata.pop("git_revision", None)
    latest.write_text(json.dumps(metadata))
    results.append(check("missing_commit_identity"))
    output = evidence / ("trace-run-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + ".json")
    output.write_text(json.dumps({"head":head, "snapshot":str(snapshot), "results":results}, ensure_ascii=False, indent=2) + "\n")
    print(f"Evidence: {output}")
    for item in results:
        print(item["scenario"], item["exit_code"])
    return 0 if all(item["exit_code"] == 1 for item in results[1:]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
