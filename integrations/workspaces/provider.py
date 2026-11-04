#!/usr/bin/env python3
import json,os,sys,time,hashlib
from pathlib import Path
import manager as m
OPERATIONS=['template.list','template.get','template.publish','template.ensure_image','provision','list','status','acquire','checkpoint','suspend','handoff','dispose','reconcile','exec','git','file.read','file.write']
def configure():
 m.ROOT.mkdir(parents=True,exist_ok=True);m.ROOT.chmod(0o700)
 p=m.ROOT/'identity.json'
 if not p.exists():m.save(p,{'node':hashlib.sha256(os.urandom(32)).hexdigest()})
 node=m.read(p)['node'];g=m.gateway();site=g.invoke('system.info',{})['site'];resource='workspace.mac.'+node[:20]
 old=m.read(m.ROOT/'provider.json') if (m.ROOT/'provider.json').exists() else {};known={o['contract'] for o in old.get('offers',[])};offers=[]
 for op in OPERATIONS:
  name='workspace.'+op;contract=name+':1';usage={'before':'Mac-bound run-owned workspace. Discover argument schemas using workspace_tools or the repository README. Every response requires result.ok inspection.','parameters':{'arguments':'Object; workspace_id, holder and generation are required for writes to acquired instances.'},'read_result':'ok=false is a business/tool error. Large results are content-addressed JSON chunks.'}
  if contract not in known:g.invoke('cap.define',{'name':name,'inputs':{'arguments':'obj'},'outputs':{'ok':'bool','result':'obj'},'description':'Docker workspace '+op,'usage':usage})
  offers.append({'contract':contract,'resource':resource,'service':'workflow-workspaces','capacity':16,'usage':usage,'argv':[sys.executable,str(m.ROOT/'provider.py'),'adapter',op]})
 cfg={'site':site,'realm':'site','resource':resource,'offers':offers,'parallelism':16,'timeout':None,'poll_seconds':2,'retry_seconds':5};m.save(m.ROOT/'provider.json',cfg);print(json.dumps({'resource':resource,'provider':node,'contracts':len(offers)}))
def main():
 mode=sys.argv[1]
 if mode=='configure':configure();return
 if mode=='monitor':
  with m.lock('lifecycle-monitor'):
   while True:m.monitor_once();time.sleep(5)
 if mode=='worker':
  sys.path.insert(0,str(m.ROOT/'runtime'));from biomerte.worker import run_worker
  cfg=m.read(m.ROOT/'provider.json');g=m.gateway()
  class Remote:
   site=cfg['site'];realm='site';node=m.read(m.ROOT/'identity.json')['node']
   def call(self,name,args,rid=None):return g.invoke(name,args,rid,self.realm)
  run_worker(str(m.ROOT),Remote(),cfg,False);return
 if mode=='adapter':
  payload=json.load(sys.stdin)
  try:
   operation=sys.argv[2];args=payload['inputs']['arguments'];schema=m.read(m.ROOT/'schemas.json')[operation]
   if any(k not in args for k in schema['required']) or any(k not in schema['properties'] for k in args):raise ValueError('Arguments do not match workspace schema')
   types={'string':str,'integer':int,'boolean':bool,'array':list,'object':dict}
   for key,value in args.items():
    if type(value) is not types[schema['properties'][key]['type']]:raise ValueError('Invalid type for '+key)
   result=m.handle(operation,args,payload['operation']);out={'ok':result.get('exit_code',0)==0,'result':result}
  except (ValueError,RuntimeError,FileNotFoundError,AssertionError) as e:out={'ok':False,'result':{'error':str(e),'kind':type(e).__name__}}
  sys.path.insert(0,str(m.GITROOT));import proxy
  print(json.dumps(proxy.compact(out)))
if __name__=='__main__':main()
