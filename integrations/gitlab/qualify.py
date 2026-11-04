import json,sys,time,urllib.request
from pathlib import Path
R=Path.home()/'.local/share/biome-gitlab';sys.path.insert(0,str(R))
import proxy
C=Path.home()/'.local/share/biome-lmstudio';cfg=json.loads((C/'coding-config.json').read_text());seq=0
def rpc(method,params):
 global seq
 seq+=1
 req=urllib.request.Request('http://127.0.0.1:'+str(cfg['mcp_port'])+'/mcp',data=json.dumps(dict(jsonrpc='2.0',id=seq,method=method,params=params)).encode(),headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','Authorization':'Bearer '+(C/'mcp-token').read_text().strip()})
 with urllib.request.urlopen(req) as r:d=proxy.decode_rpc(r.read(),r.headers.get('Content-Type',''))
 if 'error' in d:raise RuntimeError(d['error'])
 return d['result']
def call(name,args):
 d=rpc('tools/call',{'name':name,'arguments':args})
 if d.get('isError'):raise RuntimeError(d)
 return json.loads(d['content'][0]['text'])
p=R/'qualification.json';e=json.loads(p.read_text()) if p.exists() else {}
def save():proxy.atomic(p,e)
def run(label,contract,args):
 if label not in e:
  x=call('git_start',dict(contract=contract,arguments=args));e[label]={'operation':x['operation']};save()
 else:x=call('git_status',{'operation':e[label]['operation']})
 while x['state'] not in ('succeeded','failed','cancelled','uncertain'):
  time.sleep(2);x=call('git_status',{'operation':x['operation']})
 e[label]=x;save();print(label,x['state'],flush=True)
 assert x['state']=='succeeded' and x['result']['ok'],x
 return x['result']['result']
tools=rpc('tools/list',{})['tools'];e['facade_tools']=[x['name'] for x in tools];save();print('facade',e['facade_tools'],flush=True)
a=run('create','git.gitlab.api:1',dict(method='POST',path='/api/v4/projects',body=dict(name='Biome Git qualification',path='biome-git-qualification',visibility='private',initialize_with_readme=True,default_branch='main')))
project=a['data'];pid=str(project['id']);url=project['http_url_to_repo'];e['project_url']=project['web_url'];save()
base=dict(namespace='qualification',path='repo-'+pid)
run('clone','git.clone:1',dict(base,url=url))
run('branch','git.checkout:1',dict(base,options=['-b','biome-proxy-test']))
run('commit','git.commit:1',dict(base,options=['--allow-empty','-m','Qualify Biome Git capability proxy']))
run('push','git.push:1',dict(base,options=['-u','origin','biome-proxy-test']))
run('mr','git.gitlab.save_merge_request:1',dict(project_id=pid,title='Qualify Biome Git proxy',source_branch='biome-proxy-test',target_branch='main'))
run('read','git.gitlab.get_project:1',dict(project_id=pid))
run('log','git.log:1',dict(base,options=['-1','--oneline']))
print('PASS',e['project_url'],flush=True)