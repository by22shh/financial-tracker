"""Run new flow probes on a pristine HEAD archive, unique disposable DB."""
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import uuid

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
snapshot = Path(tempfile.mkdtemp(prefix="fintracker-recheck-flows-"))
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
archive = subprocess.check_output(["git", "archive", head], cwd=ROOT)
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
    tar.extractall(snapshot, filter="data")
(snapshot / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
probe = OUT / "test_new_flow_recheck.py"
shutil.copy2(probe, snapshot / "tests/readiness/test_new_flow_recheck.py")
env = {key: value for key, value in os.environ.items() if not key.startswith("FINTRACKER_")}
env.update(FINTRACKER_TEST_DB="fintracker_recheck_flows_" + uuid.uuid4().hex[:10], FINTRACKER_TEST_PG_PORT="55432", FINTRACKER_AI__ENABLED="false", FINTRACKER_ASR__PROVIDER="none", PYTHONPATH=str(snapshot / "src"))
output = OUT / ("run-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"))
output.mkdir()
command = [str(snapshot / ".venv/bin/python"), "-m", "pytest", "tests/readiness/test_new_flow_recheck.py", "-q", "--tb=short", "--junitxml=" + str(output / "junit.xml")]
(output / "scope.json").write_text(json.dumps({"head": head, "snapshot": str(snapshot), "database": env["FINTRACKER_TEST_DB"], "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest(), "command": command, "external_calls": False}, indent=2))
print(output, flush=True)
with (output / "pytest.log").open("w") as log:
    result = subprocess.run(command, cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)
print((output / "pytest.log").read_text())
raise SystemExit(result.returncode)
