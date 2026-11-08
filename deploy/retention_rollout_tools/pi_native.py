#!/usr/bin/env python3
"""Native Pi rollout probes and independent systemd job (Python 3.4)."""
import argparse,json,os,signal,subprocess,sys,time
from urllib.request import urlopen
import retention_rollout as r
import provider_gate as g
import terminal_safety
P=r.P
HOME='/home/pi';TERM=HOME+'/.local/share/biomerte-terminal'
RUNTIME=HOME+'/.local/share/biomerte-releases/8fe9a262cdb88d7bb4dc51928b01373d4b6111ae'
SERVICE='biomerte-terminal.service';CONTROLLER='biomerte.service';GATEWAYLINK='biomerte-gateway.service'
def service_for(phase):return {'controller':CONTROLLER,'gatewaylink':GATEWAYLINK,'terminal':SERVICE}[phase]
def inspect(service):
 text=subprocess.check_output(['systemctl','show',service,'--property=MainPID','--property=Restart','--property=User','--property=WorkingDirectory']).decode()
 result=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
 result['pid']=int(result.get('MainPID','0'));return result

def identity(pid):
 path='/proc/'+str(pid)
 with open(path+'/stat') as f:info=f.read()
 with open(path+'/cmdline','rb') as f:argv=f.read().decode().replace('\x00',' ').strip()
 return {'pid':pid,'start_ticks':info[info.rfind(')')+2:].split()[19],'uid':os.stat(path).st_uid,'argv':argv}

def remote():
 sys.path.insert(0,RUNTIME)
 from biomerte.worker import Remote
 cfg=r.read_json(TERM+'/manifest.json');node=r.read_json(TERM+'/node.json')
 return Remote(cfg['url'],cfg['site'],node['node'],TERM+'/node.key'),cfg

def snapshot(b,phase):
 v=inspect(service_for(phase))
 r.require(v['pid']>0,'service not running');v['identity']=identity(v['pid']);v['time']=time.time()
 if phase=='gatewaylink':
  expected=r.read_json(b/'plan.json')['phases']['gatewaylink']
  r.require(v['User']=='pi' and v['WorkingDirectory']==expected['working_directory'],'gatewaylink service target differs')
  r.require(v['identity']['uid']==os.getuid() and expected['agent_path'] in v['identity']['argv'],'gatewaylink process differs')
 if phase=='terminal':
  r.require(v['Restart']=='always' and v['User']=='pi','worker restart policy/owner differs')
  r.require(v['identity']['uid']==os.getuid() and 'biomerte' in v['identity']['argv'] and 'worker' in v['identity']['argv'],'worker identity differs')
 r.dump(b/('before-'+phase+'.json'),v)

def health(b,phase):
 before=r.read_json(b/('before-'+phase+'.json'));v=inspect(service_for(phase))
 if not v['pid'] or v['pid']==before['pid']:return {'ready':False}
 if phase in ('controller','gatewaylink'):
  with urlopen('http://127.0.0.1:8740/health',timeout=3) as response:h=json.loads(response.read().decode())
  local_ready=h.get('realms')=={'site':'ready','local':'ready'} and not h.get('background_errors')
  if phase=='gatewaylink':
   if not local_ready:return {'ready':False,'pid':v['pid']}
   expected=r.read_json(b/'plan.json')['phases']['gatewaylink'];sys.path.insert(0,expected['working_directory'])
   from biomerte.gateway_journal import GatewayJournal
   conf=r.read_json('/home/pi/.local/share/biomerte/node.json');settings=conf['gateway_journal'];status=GatewayJournal(settings,conf['site']).status()
   own=next((n for n in status.get('nodes',[]) if n['id']==settings['controller_id']),{})
   fresh=bool(own.get('enabled') and own.get('ready') and own.get('seen',0)>before['time'] and time.time()-own.get('seen',0)<status.get('heartbeat_seconds',15))
   return {'ready':fresh,'pid':v['pid'],'fresh_gateway_heartbeat':fresh}
  return {'ready':local_ready,'pid':v['pid']}
 h=r.read_json(TERM+'/worker-health.json');threshold=r.read_json(b/'restarted-terminal.json')['time']
 return {'ready':h.get('state')=='ready' and not h.get('blocked') and h.get('observed_at',0)>threshold and time.time()-h.get('observed_at',0)<30,'pid':v['pid']}

def local_safety(b):
    report=terminal_safety.inspect_local(TERM,'Linux',[inspect(SERVICE)['pid']])
    r.dump(b/'local-quiescence.json',report);return report

def await_local_safety(b):
    deadline=time.monotonic()+120
    while True:
        report=local_safety(b)
        if report['quiescent']:return
        if time.monotonic()>=deadline:raise RuntimeError('Local terminal execution unresolved: '+json.dumps(report['blocked'][:5]))
        time.sleep(1)

def run_terminal(b,sha):
 r.require(os.getuid()!=0,'run standalone job as existing pi worker owner')
 a=argparse.Namespace(bundle=str(b),plan_sha256=sha,phase='terminal');r.verified(a)
 rr,cfg=remote();originals=[]
 for o in cfg['offers']:
  originals.append({'provider':rr.node,'contract':o['contract'],'resource':o['resource'],'service':o.get('service',''),'configuration':{},'capacity':o.get('capacity',16),'state':'ready'})
 r.require(all(o['capacity']==16 and o['service']=='terminal' for o in originals),'offer fields changed')
 r.dump(b/'original-offers.json',originals);result={'state':'waiting-for-drain','time':time.time()};r.dump(b/'standalone-receipt.json',result)
 try:
  snapshot(b,'terminal');g.acquire(rr,originals,timeout=900);await_local_safety(b)
  r.dump(b/'gate.json',{'owner_pid':os.getpid(),'closed':True});r.apply(a);result['state']='applied'
 except BaseException as e:result.update(state='failed',error=str(e));raise
 finally:
  try:
   g.restore(rr,originals);r.dump(b/'gate.json',{'owner_pid':os.getpid(),'closed':False});result['offers_restored']=True
  except Exception as e:result.update(state='restore_failed',offers_restored=False,restoration_error=str(e))
  result['finished']=time.time();r.dump(b/'standalone-receipt.json',result)
  if not result.get('offers_restored'):raise RuntimeError('Offer restoration failed')

def restart(b):
 v=inspect(SERVICE);before=r.read_json(b/'before-terminal.json')
 r.require(v['pid']>0 and v['Restart']=='always' and v['User']=='pi','restart policy differs')
 ident=identity(v['pid']);r.require(ident['uid']==os.getuid() and 'biomerte' in ident['argv'] and 'worker' in ident['argv'],'worker identity differs')
 r.require(local_safety(b)['quiescent'],'local terminal execution is not quiescent')
 r.require(identity(v['pid'])==ident,'PID changed');os.kill(v['pid'],signal.SIGTERM)
 end=time.monotonic()+30
 while time.monotonic()<end:
  now=inspect(SERVICE)
  if now['pid']!=v['pid']:
   r.dump(b/'restarted-terminal.json',{'time':time.time(),'prior_pid':v['pid']});return
  time.sleep(.2)
 raise RuntimeError('Prior worker did not exit')

def main():
 p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--bundle',required=True);p.add_argument('--plan-sha256');a=p.parse_args();b=P(a.bundle).resolve()
 if a.action=='audit-local':
  r.verified(argparse.Namespace(bundle=str(b),plan_sha256=a.plan_sha256))
  worker=inspect(SERVICE)['pid']
  with open('/proc/self/cgroup') as f: own_group=f.read()
  with open('/proc/'+str(worker)+'/cgroup') as f: worker_group=f.read()
  r.require(own_group!=worker_group,'audit must run outside worker cgroup')
  deadline=time.monotonic()+60
  while True:
   report=local_safety(b)
   if report['quiescent'] or time.monotonic()>=deadline:break
   time.sleep(1)
  report.update(observed_at=time.time(),standalone_pid=os.getpid(),worker_pid=worker,outside_worker_cgroup=True)
  r.dump(b/'standalone-audit.json',report);print(json.dumps(report));return
 if a.action=='dispatch-audit':
  r.verified(argparse.Namespace(bundle=str(b),plan_sha256=a.plan_sha256))
  command=['sudo','-n','systemd-run','--unit=biomerte-retention-audit-v8','--property=User=pi','/usr/bin/python3','-B','-S',str(b/'pi_native.py'),'audit-local','--bundle',str(b),'--plan-sha256',a.plan_sha256]
  subprocess.check_call(command);print(json.dumps({'dispatched':True,'read_only_audit':True}));return
 if a.action=='run-terminal':run_terminal(b,a.plan_sha256);return
 if a.action=='dispatch':
  r.verified(argparse.Namespace(bundle=str(b),plan_sha256=a.plan_sha256))
  command=['sudo','-n','systemd-run','--unit=biomerte-retention-rollout','--property=User=pi','/usr/bin/python3','-B','-S',str(b/'pi_native.py'),'run-terminal','--bundle',str(b),'--plan-sha256',a.plan_sha256]
  subprocess.check_call(command);print(json.dumps({'dispatched':True,'unit':'biomerte-retention-rollout.service'}));return
 if a.action=='snapshot-controller':snapshot(b,'controller');return
 if a.action=='snapshot-gatewaylink':snapshot(b,'gatewaylink');return
 if a.action=='controller-health':out=health(b,'controller')
 elif a.action=='terminal-health':out=health(b,'terminal')
 elif a.action=='gatewaylink-health':out=health(b,'gatewaylink')
 elif a.action=='restart':restart(b);return
 elif a.action=='regate':
  rr,_=remote();g.acquire(rr,r.read_json(b/'original-offers.json'),timeout=60);await_local_safety(b);return
 elif a.action=='quiescence':
  marker=r.read_json(b/'gate.json');os.kill(marker['owner_pid'],0);rr,_=remote();q=g.poll(rr)
  local=local_safety(b)
  out={'admission_closed':marker['closed'] is True,'active_commands':len(q['actions'])+len(q['fences'])+len(q['activations'])+(0 if local['quiescent'] else 1),'worker_pid':inspect(SERVICE)['pid'],'local_quiescent':local['quiescent']}
 else:raise RuntimeError('unknown action')
 print(json.dumps(out))
if __name__=='__main__':main()
