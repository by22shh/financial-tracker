"""Probe freshly built image without DB/network credentials or live data."""
from pathlib import Path
import json
import subprocess
import uuid

OUT = Path(__file__).resolve().parent
IMAGE = 'fintracker:audit-5646887'
name = 'fintracker-storage-audit-' + uuid.uuid4().hex[:10]
write_code = '''import asyncio,json
from fintracker.config import Settings
from fintracker.infra.storage import build_storage
from fintracker.infra.security_log import build_security_log
s=Settings(); objects=build_storage(s.storage); journal=build_security_log(s.security_log)
async def main():
 await objects.put('audit-probe',b'only synthetic data')
 await journal._storage.put_if_absent('audit-probe','synthetic')
 print(json.dumps({'objects_written':True,'journal_written':True}))
asyncio.run(main())
'''
read_code = '''import asyncio,json
from fintracker.config import Settings
from fintracker.infra.storage import build_storage
from fintracker.infra.security_log import build_security_log
s=Settings(); objects=build_storage(s.storage); journal=build_security_log(s.security_log)
async def main():
 print(json.dumps({'objects_visible':await objects.get('audit-probe') is not None,'journal_visible':await journal._storage.get('audit-probe') is not None}))
asyncio.run(main())
'''
def run(args):
 p=subprocess.run(args,text=True,capture_output=True)
 return {'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}

result={'image':IMAGE}
try:
 result['write']=run(['docker','run','--name',name,'--entrypoint','python',IMAGE,'-c',write_code])
 if result['write']['exit_code'] == 0:
  result['mounts']=run(['docker','inspect',name,'--format','{{json .Mounts}}'])
  result['separate_process_read']=run(['docker','run','--rm','--entrypoint','python',IMAGE,'-c',read_code])
finally:
 result['cleanup']=run(['docker','rm','-v',name])
(OUT/'container-storage.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
