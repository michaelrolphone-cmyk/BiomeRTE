"""Exercise run binding, automatic suspension, recovery and terminal cleanup."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.home()/'.local/share/biome-workspaces'))
import manager as m
g = m.gateway()
path = m.ROOT/'qualification-workflow.json'
q = m.read(path) if path.exists() else {}
nodes = {'failed': {'type':'fail','reason':'Workspace qualification step failed'},
         'wait': {'type':'human','question':'Automated workspace suspension checkpoint','outputs':{},'next':'resume'},
         'end': {'type':'end','result':{}}}
def ref(s): return {'$':s}
def service(name, operation, arguments, next_node):
    nodes[name] = {'type':'service','contract':'workspace.'+operation+':1',
                   'lease_scope':'activity','args':{'arguments':arguments},
                   'save':name,'next':name+'_check'}
    nodes[name+'_check'] = {'type':'choose','cases':[{'when':{'op':'eq','args':[ref(name+'.ok'),True]},'to':next_node}],'default':'failed'}
def access(step):
    return {'workspace_id':ref(step+'.result.id'),'holder':ref(step+'.result.holder'),'generation':ref(step+'.result.generation')}
service('provision','provision',{'run_id':'$current','name':'bpmn-qualification','template':'linux-basic@1'},'acquire')
service('acquire','acquire',{'workspace_id':ref('provision.result.id'),'holder':'workflow-step-a'},'write')
service('write','exec',dict(access('acquire'),argv=['bash','-lc','echo workflow-data > /workspace/from-step-a; echo provisioned-tool > /opt/from-step-a']),'wait')
service('resume','acquire',{'workspace_id':ref('provision.result.id'),'holder':'workflow-step-b'},'verify')
service('verify','exec',dict(access('resume'),argv=['bash','-lc','test "$(cat /workspace/from-step-a)" = workflow-data && test "$(cat /opt/from-step-a)" = provisioned-tool']),'end')
graph = {'inputs':{},'outputs':{},'start':'provision','nodes':nodes}
if 'run' not in q:
    g.invoke('process.publish',{'name':'workspace-lifecycle-qualification','graph':graph},'workspace-lifecycle-publish-v1')
    q['run'] = g.invoke('process.start',{'name':'workspace-lifecycle-qualification','inputs':{}},'workspace-lifecycle-start-v1')['id']; m.save(path,q)
if not q.get('suspension'):
    while True:
        r = g.invoke('process.get',{'run':q['run']})
        assert r['state']=='running',r
        t = next((t for t in r['tokens'] if t.get('at')=='wait' and t.get('wait')),None)
        if t: break
        time.sleep(2)
    wid = t['vars']['provision']['result']['id']
    while m.state(wid)['state']!='suspended': time.sleep(2)
    w = m.state(wid)
    assert not m.exists('container',w['container']) and not m.exists('volume',w['volume'])
    q.update(workspace_id=wid,wait=t['wait'],suspension='passed');m.save(path,q)
    print('AUTOMATIC WAIT SUSPENSION PASS',flush=True)
g.invoke('process.answer',{'wait':q['wait'],'answer':{}},'workspace-lifecycle-answer-v1')
while True:
    r = g.invoke('process.get',{'run':q['run']})
    if r['state']!='running': break
    time.sleep(2)
assert r['state']=='completed',r
q['trace']=r['trace'];m.save(path,q)
while m.state(q['workspace_id'])['state']!='disposed': time.sleep(2)
w=m.state(q['workspace_id'])
assert not m.exists('container',w['container']) and not m.exists('volume',w['volume'])
q['completion_cleanup']='passed';m.save(path,q)
print('BPMN BINDING, RESUMPTION, FILES/TOOLS, NORMAL COMPLETION CLEANUP PASS',flush=True)
