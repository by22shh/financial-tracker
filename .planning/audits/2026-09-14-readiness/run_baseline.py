from pathlib import Path
import datetime as dt, hashlib, importlib.util, json, os, shutil, subprocess, tempfile, uuid
ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('capture',ROOT/'.planning/audits/2026-09-14-verification-enqwqlik/run_additional_checks.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
snapshot=Path(tempfile.mkdtemp(prefix='fintracker-readiness-'));head, hashes=module.capture(ROOT,snapshot)
(snapshot/'.venv').symlink_to(ROOT/'.venv',target_is_directory=True)
if (ROOT/'.research-private').exists():(snapshot/'.research-private').symlink_to(ROOT/'.research-private',target_is_directory=True)
for p in [Path('src/.DS_Store'),Path('src/fintracker/.DS_Store')]:
 if (ROOT/p).exists():shutil.copy2(ROOT/p,snapshot/p)
run=OUT/'baseline';run.mkdir(exist_ok=True)
suffix=uuid.uuid4().hex[:12]
env={k:v for k,v in os.environ.items() if not k.startswith('FINTRACKER_')}
env.update(FINTRACKER_TEST_DB='fintracker_ready_'+suffix,FINTRACKER_RESTORE_DB='fintracker_ready_restore_'+suffix,FINTRACKER_TEST_PG_PORT='55432',FINTRACKER_AI__ENABLED='false',FINTRACKER_ASR__PROVIDER='none',FINTRACKER_PERF='1',PYTHONPATH=str(snapshot/'src'))
(run/'scope.json').write_text(json.dumps({'head':head,'snapshot':str(snapshot),'database':env['FINTRACKER_TEST_DB'],'source_hashes':hashes,'external_calls':False},indent=2))
print(snapshot,flush=True)
commands={'format':['.venv/bin/ruff','format','--check','src','tests'],'lint':['.venv/bin/ruff','check','src','tests'],'types':['.venv/bin/mypy','src/fintracker'],'tests':['.venv/bin/python','-m','pytest','-q','--tb=short','--junitxml='+str(run/'junit.xml')]}
results={}
for name,cmd in commands.items():
 with (run/(name+'.log')).open('w') as log:result=subprocess.run(cmd,cwd=snapshot,env=env,stdout=log,stderr=subprocess.STDOUT)
 results[name]=result.returncode;print(name,result.returncode,flush=True)
for name in ['performance.json','restore_drill.json','extraction_accuracy.json']:
 source=snapshot/'.planning/evidence'/name
 if source.exists():shutil.copy2(source,run/name)
results['source_unchanged']=all((ROOT/name).exists() and hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items())
(run/'results.json').write_text(json.dumps(results,indent=2))
raise SystemExit(any(results[k]!=0 for k in commands))
