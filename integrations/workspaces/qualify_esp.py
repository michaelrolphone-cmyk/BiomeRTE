"""Verify cached ESP-IDF instance, workspace file API, and failed-run cleanup."""
import base64, sys, time
from pathlib import Path
sys.path.insert(0,str(Path.home()/'.local/share/biome-workspaces'))
import manager as m
import workspace_agent_tools as agent
g=m.gateway();path=m.ROOT/'qualification-esp.json';q=m.read(path) if path.exists() else {}
def call(label,operation,args):
 if label not in q:
  d=agent.workspace_start({'contract':'workspace.'+operation+':1','arguments':args});q[label]={'id':d['id']};m.save(path,q)
 while True:
  d=agent.workspace_status({'operation':q[label]['id']})
  if d['state'] in ('succeeded','failed','uncertain'):break
  time.sleep(2)
 q[label]=d;m.save(path,q);assert d['state']=='succeeded' and d['result']['ok'],d
 print(label,'PASS',flush=True);return d['result']['result']
if 'run' not in q:
 q['run']=g.invoke('process.start',{'name':'workspace-qualification-hold','inputs':{}},'ws-esp-instance-run')['id'];m.save(path,q)
image=call('cached_image','template.ensure_image',{'template':'esp-idf@2'});assert image['reused']
w=call('provision','provision',{'run_id':q['run'],'name':'esp-instance','template':'esp-idf@2','auto_suspend':False})
assert w['image']==image['image']
a=call('acquire','acquire',{'workspace_id':w['id'],'holder':'esp-agent'})
access={'workspace_id':a['id'],'holder':a['holder'],'generation':a['generation']}
call('tools','exec',dict(access,argv=['bash','-c','idf.py --version && qemu-system-xtensa --version && qemu-system-riscv32 --version']))
payload=base64.b64encode(b'ESP workspace file round trip\x00\xff').decode()
call('file_write','file.write',dict(access,path='qualification/payload.bin',data_base64=payload))
got=call('file_read','file.read',dict(access,path='qualification/payload.bin'));assert got['stdout'].strip()==payload
g.invoke('process.cancel',{'run':q['run']},'ws-esp-instance-cancel')
while m.state(w['id'])['state']!='disposed':time.sleep(2)
assert m.exists('image',image['image']) and not m.exists('container',w['container']) and not m.exists('volume',w['volume'])
q['esp_cleanup']='passed';m.save(path,q)
graph={'inputs':{},'outputs':{},'start':'provision','nodes':{'provision':{'type':'service','contract':'workspace.provision:1','lease_scope':'activity','args':{'arguments':{'run_id':'$current','name':'failure-cleanup'}},'save':'workspace','next':'failure'},'failure':{'type':'fail','reason':'Expected qualification failure'}}}
if 'failed_run' not in q:
 g.invoke('process.publish',{'name':'workspace-failure-qualification','graph':graph},'ws-failure-publish')
 q['failed_run']=g.invoke('process.start',{'name':'workspace-failure-qualification','inputs':{}},'ws-failure-start')['id'];m.save(path,q)
while True:
 r=g.invoke('process.get',{'run':q['failed_run']})
 if r['state']!='running':break
 time.sleep(2)
assert r['state']=='failed' and r['trace'][0]['result']['ok'],r
wid=r['trace'][0]['result']['result']['id']
while m.state(wid)['state']!='disposed':time.sleep(2)
assert not m.exists('volume',m.state(wid)['volume'])
q['failed_run_cleanup']='passed';m.save(path,q)
print('ESP-IDF CACHED INSTANCE, BINARY FILE ROUND TRIP, FAILED RUN CLEANUP PASS',flush=True)
