"""Index final runs only; never rewrites existing release evidence."""
from pathlib import Path
import datetime as dt
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SELECTED = {
    'baseline/junit.xml': 'baseline',
    'flows/run-20260914T114345Z/junit.xml': 'new_required_behavior',
    'money/run-20260914T114342Z/junit.xml': 'new_required_behavior',
    'async/run-20260914T114344Z/junit.xml': 'new_required_behavior',
    'ops/junit.xml': 'new_required_behavior',
    'ops/retention/junit.xml': 'new_required_behavior',
}

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

runs = []
for name, kind in SELECTED.items():
    path = HERE/name
    doc = ET.parse(path)
    cases = doc.findall('.//testcase')
    failed = len(doc.findall('.//failure'))
    errors = len(doc.findall('.//error'))
    skipped = len(doc.findall('.//skipped'))
    runs.append({
        'artifact': name, 'kind': kind, 'tests': len(cases),
        'passed': len(cases)-failed-errors-skipped,
        'failed': failed, 'errors': errors, 'skipped': skipped,
        'sha256': digest(path),
    })
new = [r for r in runs if r['kind'] == 'new_required_behavior']
assert sum(r['tests'] for r in new) == 34
assert sum(r['failed'] for r in new) == 30
assert sum(r['passed'] for r in new) == 4
assert not any(r['errors'] or r['skipped'] for r in new)
scope = json.loads((HERE/'baseline/scope.json').read_text())
changed = [name for name, hash_value in scope['source_hashes'].items()
           if not (ROOT/name).is_file() or digest(ROOT/name) != hash_value]
head = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
assert not changed, changed
assert head == scope['head'], 'Commit changed during recheck'
report = {
    'checked_commit': head,
    'comparison_base': '7a56769e2a7dc465a4985ce2755d398d08b0f3de',
    'compiled_at': dt.datetime.now(dt.UTC).isoformat(),
    'verdict': 'gaps_found',
    'captured_sources_unchanged': True,
    'external_live_calls': False,
    'runs': runs,
    'additional_total': 34,
    'additional_failed': 30,
    'additional_passed': 4,
    'excluded_preliminary': [
        'flows/run-20260914T114302Z',
        'async/run-20260914T114327Z',
    ],
    'main_report': 'docs/READINESS_RECHECK_5646887.md',
    'artifacts': {},
}
for path in sorted(HERE.rglob('*')):
    if path.is_file() and '__pycache__' not in path.parts and path.name != 'evidence-index.json':
        report['artifacts'][str(path.relative_to(HERE))] = digest(path)
(HERE/'evidence-index.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'runs': runs, 'captured_sources_unchanged': True},ensure_ascii=False))
