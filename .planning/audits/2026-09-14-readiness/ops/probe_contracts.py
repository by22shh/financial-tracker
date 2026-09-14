"""Read-only independent checks against actual emitted schemas/container defaults."""
import io,json,os,subprocess,tarfile,tempfile
from pathlib import Path
from fintracker.infra.ai.schemas import json_schema_for,ExtractionResponse,ReceiptResponse,RecommendationResponse,AnalyticsPlan
ROOT=Path(__file__).resolve().parents[4];OUT=Path(__file__).resolve().parent
report={}
for model in (ExtractionResponse,ReceiptResponse,RecommendationResponse,AnalyticsPlan):
 schema=json_schema_for(model);violations=[]
 def walk(node,path='$'):
  if isinstance(node,dict):
   if node.get('type')=='object':
    missing=set(node.get('properties',{}))-set(node.get('required',[]))
    if missing:violations.append({'path':path,'missing_required':sorted(missing)})
   for k,v in node.items():walk(v,path+'.'+k)
  elif isinstance(node,list):
   for i,v in enumerate(node):walk(v,path+f'[{i}]')
 walk(schema)
 report[model.__name__]={'compatible_with_documented_strict_required':not violations,'violations':violations}
 (OUT/(model.__name__+'.schema.json')).write_text(json.dumps(schema,indent=2))
command=['docker','run','--rm','--entrypoint','python','fintracker:local','-c',"import json; from fintracker.config import Settings; from fintracker.infra.storage import build_storage; from fintracker.infra.security_log import build_security_log; s=Settings(); results={};\nfor name,fn,cfg in [('objects',build_storage,s.storage),('security_log',build_security_log,s.security_log)]:\n try:fn(cfg);results[name]='ok'\n except Exception as e:results[name]=type(e).__name__+': '+str(e)\nprint(json.dumps(results))"]
result=subprocess.run(command,capture_output=True,text=True)
report['container_storage_defaults']={'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
snapshot=Path(tempfile.mkdtemp(prefix='fintracker-ci-clean-'))
archive=subprocess.check_output(['git','archive','HEAD'],cwd=ROOT)
with tarfile.open(fileobj=io.BytesIO(archive)) as tf:tf.extractall(snapshot,filter='data')
env={**os.environ,'GIT_DIR':str(ROOT/'.git'),'GIT_WORK_TREE':str(snapshot)}
result=subprocess.run([str(ROOT/'.venv/bin/python'),str(snapshot/'.planning/tools/check_traceability.py')],cwd=snapshot,env=env,capture_output=True,text=True)
report['ci_clean_checkout']={'snapshot':str(snapshot),'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
(OUT/'contracts.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
