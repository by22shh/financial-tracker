from pathlib import Path
import importlib.util,os,shutil,subprocess,tempfile,uuid,json
ROOT=Path(__file__).resolve().parents[4];PROBES=Path(__file__).resolve().parent;OUT=PROBES/'retention';OUT.mkdir(exist_ok=True)
spec=importlib.util.spec_from_file_location('capture',ROOT/'.planning/audits/2026-09-14-verification-enqwqlik/run_additional_checks.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
snapshot=Path(tempfile.mkdtemp(prefix='fintracker-ops-readiness-'));head,hashes=m.capture(ROOT,snapshot)
(snapshot/'.venv').symlink_to(ROOT/'.venv',target_is_directory=True)
shutil.copy2(PROBES/'test_media_retention.py',snapshot/'tests/integration/test_media_retention.py')
env={k:v for k,v in os.environ.items() if not k.startswith('FINTRACKER_')};env.update(FINTRACKER_TEST_DB='fintracker_ops_recheck_'+uuid.uuid4().hex[:10],FINTRACKER_TEST_PG_PORT='55432',FINTRACKER_AI__ENABLED='false',FINTRACKER_ASR__PROVIDER='none',PYTHONPATH=str(snapshot/'src'))
(OUT/'scope.json').write_text(json.dumps({'head':head,'snapshot':str(snapshot),'database':env['FINTRACKER_TEST_DB'],'source_hashes':hashes,'external_calls':False},indent=2))
with (OUT/'pytest.log').open('w') as log:r=subprocess.run([str(snapshot/'.venv/bin/python'),'-m','pytest','-q','--tb=short','tests/integration/test_media_retention.py','--junitxml='+str(OUT/'junit.xml')],cwd=snapshot,env=env,stdout=log,stderr=subprocess.STDOUT)
print(r.returncode, OUT/'pytest.log')
raise SystemExit(r.returncode)
