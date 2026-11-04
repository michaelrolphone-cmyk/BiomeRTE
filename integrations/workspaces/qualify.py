"""Live qualification through the real Biome capability path; durable resumable receipts."""
import json,sys,time,uuid,base64
from pathlib import Path
sys.path.insert(0,str(Path.home()/'.local/share/biome-workspaces'))
import manager as m
import workspace_agent_tools as agent
path=m.ROOT/'qualification.json';q=m.read(path) if path.exists() else {}
def save():m.save(path,q)
def call(label,op,args,expected=True):
 if '--retry-failed' in sys.argv and label in q and q[label].get('state')=='succeeded' and q[label].get('result',{}).get('ok')!=expected:
  q.setdefault('prior_attempts',[]).append(dict(q.pop(label),label=label));save()
 if label not in q:
  d=agent.workspace_start({'contract':'workspace.'+op+':1','arguments':args});q[label]={'id':d['id']};save()
 while True:
  d=agent.workspace_status({'operation':q[label]['id']})
  if d['state'] in ('succeeded','failed','uncertain'):break
  time.sleep(2)
 q[label]={'id':d['id'],'state':d['state'],'result':d.get('result')};save()
 assert d['state']=='succeeded',d
 assert d['result']['ok']==expected,d
 print(label,'PASS',flush=True);return d['result']['result']
def bind(w):return {'workspace_id':w['id'],'holder':w['holder'],'generation':w['generation']}
g=m.gateway()
if 'run' not in q:
 graph={'inputs':{},'outputs':{},'start':'wait','nodes':{'wait':{'type':'human','question':'Workspace qualification hold','outputs':{},'next':'end'},'end':{'type':'end','result':{}}}}
 g.invoke('process.publish',{'name':'workspace-qualification-hold','graph':graph},'ws-qualify-process-publish')
 q['run']=g.invoke('process.start',{'name':'workspace-qualification-hold','inputs':{}},'ws-qualify-process-start')['id'];save()
image=call('image','template.ensure_image',{'template':'linux-basic@1'})
cached=call('image_cached','template.ensure_image',{'template':'linux-basic@1'});assert cached['reused'] and cached['image']==image['image']
w=call('provision','provision',{'run_id':q['run'],'auto_suspend':False})
a=call('acquire_a','acquire',{'workspace_id':w['id'],'holder':'agent-a'})
call('write','exec',dict(bind(a),argv=['bash','-lc','printf shared-work > /workspace/file.txt; printf installed-tool > /opt/agent-tool']))
b=call('handoff','handoff',dict(bind(a),to_holder='agent-b',context={'next':'Check preserved work'}))
call('stale','exec',dict(bind(a),argv=['true']),expected=False)
call('verify_handoff','exec',dict(bind(b),argv=['bash','-lc','test "$(cat /workspace/file.txt)" = shared-work && test "$(cat /opt/agent-tool)" = installed-tool']))
call('suspend','suspend',bind(b));assert not m.exists('container',w['container']) and not m.exists('volume',w['volume'])
c=call('recover','acquire',{'workspace_id':w['id'],'holder':'agent-c'})
call('verify_recovery','exec',dict(bind(c),argv=['bash','-lc','test "$(cat /workspace/file.txt)" = shared-work && test "$(cat /opt/agent-tool)" = installed-tool']))
call('git','git',dict(bind(c),argv=['ls-remote','https://Micahels-Mac-mini.local:8443/biome-agent/biome-git-qualification.git']))
g.invoke('process.cancel',{'run':q['run']},'ws-qualify-process-cancel')
while m.state(w['id'])['state']!='disposed':time.sleep(2)
assert not m.exists('container',w['container']) and not m.exists('volume',w['volume'])
assert m.exists('image',image['image'])
q['cleanup']='passed';save();print('TERMINAL CLEANUP PASS; SHARED TEMPLATE RETAINED',flush=True)
