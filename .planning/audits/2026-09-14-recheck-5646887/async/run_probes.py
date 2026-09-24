"""Execute new async boundary probes on immutable source snapshot and unique DB."""
from pathlib import Path
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import uuid

root = Path(__file__).resolve().parents[4]
evidence = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("prior_runner", root/".planning/audits/2026-09-14-verification-enqwqlik/run_fixed_checks.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
snapshot = Path(tempfile.mkdtemp(prefix="fintracker-recheck-async-"))
head, hashes = runner.capture(root, snapshot)
(snapshot/".venv").symlink_to(root/".venv", target_is_directory=True)
shutil.copy2(evidence/"test_async_recheck.py", snapshot/"tests/integration/test_async_recheck.py")
suffix = uuid.uuid4().hex[:12]
env = {key:value for key,value in os.environ.items() if not key.startswith("FINTRACKER_")}
env.update(FINTRACKER_TEST_DB="fintracker_recheck_async_"+suffix, FINTRACKER_TEST_PG_PORT="55432", FINTRACKER_TEST_PG_HOST="localhost", FINTRACKER_AI__ENABLED="false", FINTRACKER_ASR__PROVIDER="none", PYTHONPATH=str(snapshot/"src"))
output=evidence/("run-"+dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"))
output.mkdir()
(output/"scope.json").write_text(json.dumps({"head":head,"snapshot":str(snapshot),"test_database":env["FINTRACKER_TEST_DB"],"source_hashes":hashes,"external_calls":False},indent=2)+"\n")
print(f"Snapshot: {snapshot}; evidence: {output}",flush=True)
with (output/"pytest.log").open("w") as log:
    result=subprocess.run([str(snapshot/".venv/bin/python"),"-m","pytest","tests/integration/test_async_recheck.py","-q","--tb=short","--junitxml="+str(output/"junit.xml")],cwd=snapshot,env=env,stdout=log,stderr=subprocess.STDOUT)
print((output/"pytest.log").read_text())
raise SystemExit(result.returncode)
