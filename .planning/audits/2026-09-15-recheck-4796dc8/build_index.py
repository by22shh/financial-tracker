"""Generate a compact, reproducible index for this read-only audit."""

import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[3]
audit = Path(__file__).resolve().parent
sys.path.insert(0, str(root / ".planning" / "tools"))
from evidence_source import capture_source

runs = {}
for name in ("baseline", "previous", "old_money", "old_rest", "followups"):
    report = audit / f"{name}.xml"
    cases = list(ET.parse(report).iter("testcase"))
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures = []
    for case in cases:
        outcome = (
            "errors" if case.find("error") is not None else
            "failed" if case.find("failure") is not None else
            "skipped" if case.find("skipped") is not None else "passed"
        )
        counts[outcome] += 1
        if outcome in {"failed", "errors"}:
            failures.append(f"{case.get('classname')}::{case.get('name')}")
    runs[name] = {**counts, "failures": failures}

artifacts = {}
for path in sorted(audit.iterdir()):
    if path.is_file() and path.name != "evidence-index.json":
        artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
report = root / "docs" / "READINESS_RECHECK_4796dc8.md"
artifacts[str(report.relative_to(root))] = hashlib.sha256(report.read_bytes()).hexdigest()
index = {
    "checked_at": dt.datetime.now(dt.UTC).isoformat(),
    "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    "runs": runs,
    "source_at_completion": capture_source(root),
    "tracked_changes_at_completion": subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=root, text=True).splitlines(),
    "scope": "Product source and official tests unchanged. New artifacts only. Live services and performance not exercised.",
    "artifacts_sha256": artifacts,
}
(audit / "evidence-index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
print(json.dumps({"commit": index["commit"], "runs": {name: {k: v for k, v in run.items() if k != "failures"} for name, run in runs.items()}, "tracked_changes": index["tracked_changes_at_completion"]}, indent=2))
