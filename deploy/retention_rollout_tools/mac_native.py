#!/usr/bin/env python3
"""Mac-specific standalone rollout wrapper and native probes. No enrollment."""
import argparse,json,os,pathlib,plistlib,re,shlex,signal,subprocess,sys,time,urllib.request
import retention_rollout as r
import provider_gate as g
import terminal_safety
import mac_independent_services as independent_services
P=pathlib.Path
HOME=P('/Users/micahelbyrns');TERM=HOME/'.local/share/biome-mac-terminal'
PYTHON=str(HOME/'miniconda3/bin/python3')
SERVICE='system/org.biomerte.mac-terminal';CONTROLLER='gui/501/org.biomerte.controller'
GATEWAYLINK='gui/501/org.biomerte.controller-link'
LINK_AGENT=str(HOME/'.local/share/biomerte-releases/lowlat-20261104/deploy/gateway-agent.py')
AUDIT_LABEL='org.biomerte.retention-audit-v11'
def service_for(phase):return {'controller':CONTROLLER,'gatewaylink':GATEWAYLINK,'terminal':SERVICE}[phase]
def inspect(service):
 p=subprocess.run(['/bin/launchctl','print',service],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if p.returncode: return None
 text=p.stdout.decode();pid=re.search(r'^\s*pid = (\d+)\s*$',text,re.M);path=re.search(r'^\s*path = (.+)\s*$',text,re.M)
 return {'pid':int(pid.group(1)) if pid else 0,'path':path.group(1).strip() if path else None}
def remote():
 sys.path.insert(0,str(TERM));sys.path.insert(0,str(TERM/'runtime'))
 import mac_provider
 # This short-lived helper uses the installed authenticated HTTP fallback.
 # Do not alter the provider daemon or any saved transport preference.
 mac_provider._persistent_after=float('inf')
 manifest=json.loads((TERM/'terminal-manifest.json').read_text())
 return mac_provider.Remote(manifest),manifest

def snapshot(b,phase):
 v=inspect(service_for(phase))
 r.require(v and v['pid']>0,'service not running')
 if phase=='gatewaylink':
  r.require(v['path'] and P(v['path']).is_file(),'missing original gatewaylink plist')
  original=P(v['path']).read_bytes();pl=plistlib.loads(original)
  r.require(pl['Label']=='org.biomerte.controller-link' and pl.get('KeepAlive') is True,'gatewaylink restart policy differs')
  r.require(LINK_AGENT in pl['ProgramArguments'] and pl.get('WorkingDirectory')==str(P(LINK_AGENT).parent.parent),'gatewaylink target differs')
  identity=subprocess.check_output(['/bin/ps','-p',str(v['pid']),'-o','uid=,lstart=,command=']).decode().strip()
  r.require(identity.split()[0]=='501' and LINK_AGENT in identity,'gatewaylink PID identity differs')
  v['identity']=identity;v['plist_sha256']=r.digest(original)
 if phase=='terminal':
  r.require(v['path'] and P(v['path']).is_file(),'missing original launchd plist')
  original=P(v['path']).read_bytes();pl=plistlib.loads(original)
  r.require(pl['Label']=='org.biomerte.mac-terminal','wrong plist')
  r.require(pl.get('KeepAlive') is True,'restart-on-SIGTERM policy not verified')
  identity=subprocess.check_output(['/bin/ps','-p',str(v['pid']),'-o','uid=,lstart=,command=']).decode().strip()
  r.require(identity.split()[0]=='501' and str(TERM/'mac_provider.py') in identity,'worker PID identity differs')
  v['identity']=identity
  r.write(b/'original-terminal.plist',original)
 v['time']=time.time();r.dump(b/('before-'+phase+'.json'),v)

def worker_command(command):
    # Match executable/argument positions, never a substring in a shell command.
    return shlex.split(command)==[PYTHON,str(TERM/'mac_provider.py'),'worker']

def caffeinate_command(process):
    argv=shlex.split(process['command'])
    names=('caffeinate','/usr/bin/caffeinate')
    executable=process['executable'];absolute='/usr/bin/caffeinate'
    # Darwin ps truncates this non-final comm field to 16 characters even
    # with -ww. Accept that exact-length prefix only with the full absolute
    # argv[0]; a different prefix or two abbreviated identities still fails.
    truncated=(len(executable)==16 and absolute.startswith(executable)
               and bool(argv) and argv[0]==absolute)
    return (len(argv)==5 and (executable in names or truncated) and argv[0] in names
            and (absolute in (executable,argv[0])) and argv[1]=='-is'
            and argv[2:]==[PYTHON,str(TERM/'mac_provider.py'),'worker'])

def verified_worker_tree(pid):
    rows=subprocess.check_output(['/bin/ps','-ww','-axo','pid=,ppid=,pgid=,uid=,lstart=,comm=,command=']).decode().splitlines()
    processes={}
    for row in rows:
        fields=row.split(None,10)
        r.require(len(fields)==11,'incomplete worker process identity')
        item=dict(zip(('pid','ppid','pgid','uid'),map(int,fields[:4])))
        item.update(started=' '.join(fields[4:9]),executable=fields[9],command=fields[10])
        processes[item['pid']]=item
    root=processes.get(pid);r.require(root and root['uid']==501,'worker PID target changed')
    children=[p for p in processes.values() if p['ppid']==pid]
    def same_service(child):
        return child['uid']==root['uid'] and child['pgid']==root['pgid'] and child['started']==root['started']
    roots=[pid];worker=root
    if caffeinate_command(root):
        # Support wrapper -> worker as well as Darwin's worker -> watchdog fork.
        r.require(len(children)==1 and same_service(children[0]) and worker_command(children[0]['command']),'unexpected caffeinate child tree')
        worker=children[0];roots.append(worker['pid'])
    else:
        r.require(worker_command(root['command']),'worker PID target changed')
        watchdogs=[p for p in children if caffeinate_command(p) and same_service(p)]
        r.require(len(watchdogs)<=1,'multiple caffeinate watchdogs')
        roots.extend(p['pid'] for p in watchdogs)
        # All other children remain outside roots and block the safety audit.
    return {'roots':roots,'worker':worker,'root':root,'identities':{p:processes[p] for p in roots}}

def restart_safety_mode(b):
    path=b/'plan.json'
    mode=r.read_json(path).get('phases',{}).get('terminal',{}).get('restart_safety_mode') if path.is_file() else None
    r.require(mode in (None,'process-boundary'),'unsupported Mac restart safety mode')
    return mode

def restart_ready(b,report):
    mode=restart_safety_mode(b)
    if mode is None:return report.get('quiescent') is True
    fingerprint=report.get('evidence_fingerprint')
    return (report.get('restart_safety_mode')==mode and report.get('restart_safe') is True
            and isinstance(fingerprint,str) and len(fingerprint)==64 and all(c in '0123456789abcdef' for c in fingerprint))

def local_safety(b):
    service=inspect(SERVICE);r.require(service and service['pid']>0,'worker absent')
    tree=verified_worker_tree(service['pid']);roots=tree['roots'];mode=restart_safety_mode(b)
    independent=independent_services.collect(str(TERM),roots) if mode else {}
    report=(terminal_safety.inspect_local(str(TERM),'Darwin',roots,restart_safety_mode=mode,independent_process_proofs=independent) if mode
            else terminal_safety.inspect_local(str(TERM),'Darwin',roots))
    if mode:
        r.require(independent_services.verify(independent,roots),'Independent OS service identity changed during proof')
        report['independent_services']=independent
        current=inspect(SERVICE)
        r.require(current and current['pid']==service['pid'] and verified_worker_tree(service['pid'])==tree,'worker/watchdog identity changed during restart proof')
    r.dump(b/'local-quiescence.json',report);return report

def await_local_safety(b):
    deadline=time.monotonic()+120
    while True:
        report=local_safety(b)
        if restart_ready(b,report):return
        if time.monotonic()>=deadline:raise RuntimeError('Local terminal execution unresolved: '+json.dumps(report['blocked'][:5]))
        time.sleep(1)

def closed_probe(b):
 marker=r.read_json(b/'gate.json');r.require(marker['owner_pid']>0,'missing gate owner')
 os.kill(marker['owner_pid'],0)
 rr,_=remote();q=g.poll(rr);local=local_safety(b);mode=restart_safety_mode(b);safe=restart_ready(b,local)
 active=len(q['actions'])+len(q['fences'])+len(q['activations'])+(0 if safe else 1)
 if mode:
  after=g.poll(rr);active+=len(after['actions'])+len(after['fences'])+len(after['activations'])
 out={'admission_closed':marker['closed'] is True,'active_commands':active,'worker_pid':inspect(SERVICE)['pid'] if inspect(SERVICE) else 0,'local_quiescent':local['quiescent']}
 if mode:out.update(restart_safety_mode=mode,local_restart_safe=safe,unresolved_history_count=len(local.get('unresolved_history',[])),evidence_fingerprint=local.get('evidence_fingerprint'))
 return out

def gateway_heartbeat_ready(status,controller_id,after,now):
 own=next((n for n in status.get('nodes',[]) if n.get('id')==controller_id),{})
 return bool(own.get('enabled') and own.get('ready') and own.get('seen',0)>after
             and 0<=now-own.get('seen',0)<status.get('heartbeat_seconds',12))

def health(b,phase):
 before=r.read_json(b/('before-'+phase+'.json'));v=inspect(service_for(phase))
 if not v or not v['pid'] or v['pid']==before['pid']:return {'ready':False}
 if phase in ('controller','gatewaylink'):
  with urllib.request.urlopen('http://127.0.0.1:8740/health',timeout=3) as response:h=json.load(response)
  local_ready=h.get('realms')=={'site':'ready','local':'ready'} and not h.get('background_errors')
  if phase=='gatewaylink':
   if not local_ready:return {'ready':False,'pid':v['pid']}
   node=r.read_json(HOME/'.local/share/biomerte-controller/node.json')
   sys.path.insert(0,str(HOME/'.local/share/biomerte-releases/assistant-20261104-r6'))
   from biomerte.gateway_journal import GatewayJournal
   settings=node['gateway_journal'];status=GatewayJournal(settings,node['site']).status()
   fresh=gateway_heartbeat_ready(status,settings['controller_id'],before['time'],time.time())
   return {'ready':fresh,'pid':v['pid'],'fresh_gateway_heartbeat':fresh}
  return {'ready':local_ready,'pid':v['pid']}
 preserved=b/'independent-services-before.json'
 if preserved.is_file() and not independent_services.verify(r.read_json(preserved),set(verified_worker_tree(v['pid'])['roots'])):return {'ready':False,'independent_services_preserved':False}
 h=r.read_json(TERM/'worker-health.json')
 threshold=max(before['time'],r.read_json(b/'restarted-terminal.json')['time'])
 return {'ready':h.get('state')=='ready' and not h.get('blocked') and h.get('observed_at',0)>threshold and time.time()-h.get('observed_at',0)<30,'pid':v['pid']}

def qualify_gate(b,sha):
 audit_local(b,sha)
 rr,manifest=remote()
 originals=[{'provider':rr.node,'contract':o['contract'],'resource':o['resource'],'service':o.get('service',''),'configuration':{},'capacity':o.get('capacity',16),'state':'ready'} for o in manifest['offers']]
 r.require(all(o['capacity']==16 and o['service']=='terminal' for o in originals),'offer fields changed')
 started=time.monotonic();out={'passed':False,'time':time.time()}
 try:
  g.acquire(rr,originals,timeout=30)
  r.dump(b/'gate.json',{'owner_pid':os.getpid(),'closed':True})
  proof=closed_probe(b)
  r.require(r.terminal_gate_ready(r.read_json(b/'plan.json')['phases']['terminal'],proof,'Darwin'),'gate integration proof failed')
  out.update(passed=True,proof=proof)
 finally:
  try:g.restore(rr,originals);out['offers_restored']=True
  finally:
   r.dump(b/'gate.json',{'owner_pid':os.getpid(),'closed':False})
   out['elapsed_seconds']=time.monotonic()-started;r.dump(b/'gate-qualification.json',out)
 print(json.dumps(out))

def run_terminal(b,sha,preflight_only=False):
 r.require(os.getuid()==501,'standalone job must run as existing worker owner')
 a=argparse.Namespace(bundle=str(b),plan_sha256=sha,phase='terminal')
 r.verified(a)
 rr,manifest=remote()
 originals=[]
 for o in manifest['offers']:
  originals.append({'provider':rr.node,'contract':o['contract'],'resource':o['resource'],'service':o.get('service',''),'configuration':{},'capacity':o.get('capacity',16),'state':'ready'})
 # Existing offers' configuration={}, capacity=16 and ready state verified by parent.
 r.require(all(o['capacity']==16 and o['service']=='terminal' for o in originals),'offer fields changed')
 r.dump(b/'original-offers.json',originals)
 result={'state':'waiting-for-drain','time':time.time()};r.dump(b/'standalone-receipt.json',result)
 paused=False
 try:
  snapshot(b,'terminal')
  # Includes the dispatch invocation itself, so no arbitrary sleep or self-kill.
  g.acquire(rr,originals,timeout=900);paused=True;await_local_safety(b)
  r.dump(b/'gate.json',{'owner_pid':os.getpid(),'closed':True})
  r.apply(a,preflight_only=preflight_only)
  result['state']='qualified' if preflight_only else 'applied'
 except BaseException as e:
  result.update(state='failed',error=str(e));raise
 finally:
  try:
   # acquire already restores on failure; also restore if any result is uncertain.
   g.restore(rr,originals)
   r.dump(b/'gate.json',{'owner_pid':os.getpid(),'closed':False})
   result['offers_restored']=True
  except Exception as e:result.update(offers_restored=False,restoration_error=str(e),state='restore_failed')
  result['finished']=time.time();r.dump(b/'standalone-receipt.json',result)
  if not result.get('offers_restored'):raise RuntimeError('Offer restoration failed; inspect standalone receipt')

def dispatch(b,sha):
 a=argparse.Namespace(bundle=str(b),plan_sha256=sha);r.verified(a)
 # Explicit, root-managed independent service. A child nohup is insufficient.
 label='org.biomerte.retention-rollout'
 r.require(inspect('gui/501/'+label) is None,'standalone rollout job already loaded')
 path=b/'standalone.plist'
 value={'Label':label,'ProgramArguments':[PYTHON,'-B','-S',str(b/'mac_native.py'),'run-terminal','--bundle',str(b),'--plan-sha256',sha],'RunAtLoad':True,'ProcessType':'Background','StandardOutPath':str(b/'standalone.stdout'),'StandardErrorPath':str(b/'standalone.stderr')}
 r.write(path,plistlib.dumps(value))
 r.require(os.getuid()==501,'dispatch must use existing worker owner')
 subprocess.check_call(['/bin/launchctl','bootstrap','gui/501',str(path)])
 print(json.dumps({'dispatched':True,'job':'gui/501/'+label,'receipt':str(b/'standalone-receipt.json')}))

def dispatch_audit(b,sha):
 r.verified(argparse.Namespace(bundle=str(b),plan_sha256=sha))
 r.require(os.getuid()==501,'audit dispatch must use existing worker owner')
 r.require(inspect('gui/501/'+AUDIT_LABEL) is None,'standalone audit job already loaded')
 path=b/'standalone-audit.plist'
 value={'Label':AUDIT_LABEL,'ProgramArguments':[PYTHON,'-B','-S',str(b/'mac_native.py'),'audit-local','--bundle',str(b),'--plan-sha256',sha],
        'RunAtLoad':True,'ProcessType':'Background','StandardOutPath':str(b/'standalone-audit.stdout'),'StandardErrorPath':str(b/'standalone-audit.stderr')}
 r.write(path,plistlib.dumps(value))
 subprocess.check_call(['/bin/launchctl','bootstrap','gui/501',str(path)])
 print(json.dumps({'dispatched':True,'job':'gui/501/'+AUDIT_LABEL,'receipt':str(b/'standalone-audit.json')}))

def audit_local(b,sha):
 r.verified(argparse.Namespace(bundle=str(b),plan_sha256=sha))
 r.require(os.getuid()==501,'standalone audit must run as existing worker owner')
 job=inspect('gui/501/'+AUDIT_LABEL);pid=os.getpid()
 r.require(job and job['pid']==pid,'audit is not the dedicated independent launchd job')
 service=inspect(SERVICE);r.require(service and service['pid']>0,'worker absent during audit')
 roots=set(verified_worker_tree(service['pid'])['roots']);snap=terminal_safety.process_snapshot('Darwin');processes=snap['processes']
 r.require(pid in processes and all(root in processes for root in roots),'incomplete audit ancestry snapshot')
 ancestors=set();current=pid
 while current in processes and current not in ancestors:
  ancestors.add(current);current=processes[current]['ppid']
 r.require(not (ancestors&roots) and processes[pid]['pgid'] not in {processes[root]['pgid'] for root in roots},'audit is inside worker ancestry or process group')
 deadline=time.monotonic()+60
 while True:
  try:report=local_safety(b)
  except Exception as error:report={'quiescent':False,'blocked':[{'operation':'inspection','reason':type(error).__name__+': '+str(error)}],'checked_receipts':0}
  if restart_ready(b,report) or time.monotonic()>=deadline:break
  time.sleep(1)
 blocked=report['blocked'];adopted=report.get('adopted_reconciliations',[])
 out={'quiescent':report['quiescent'],'blocked_count':len(blocked),'blocked':blocked[:20],'checked_receipts':report['checked_receipts'],
      'adopted_reconciliation_count':len(adopted),'adopted_reconciliations':adopted[:20],
      'outside_worker_process':True,'standalone_pid':pid,'worker_pid':service['pid'],'observed_at':time.time(),'plan_sha256':sha}
 if restart_safety_mode(b):
  history=report.get('unresolved_history',[])
  out.update(restart_safety_mode=restart_safety_mode(b),restart_safe=restart_ready(b,report),effects_quiescent=report.get('effects_quiescent',False),
             unresolved_history_count=len(history),unresolved_history=history[:20],evidence_fingerprint=report.get('evidence_fingerprint'))
 r.dump(b/'standalone-audit.json',out);print(json.dumps(out,sort_keys=True))
 r.require(restart_ready(b,report),'Independent audit remains blocked; inspect standalone-audit.json')

def main():
 p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--bundle',required=True);p.add_argument('--plan-sha256');a=p.parse_args();b=P(a.bundle).resolve()
 if a.action=='run-terminal-preflight':run_terminal(b,a.plan_sha256,preflight_only=True);return
 if a.action=='run-terminal':run_terminal(b,a.plan_sha256);return
 if a.action=='dispatch':dispatch(b,a.plan_sha256);return
 if a.action=='qualify-gate':qualify_gate(b,a.plan_sha256);return
 if a.action=='dispatch-audit':dispatch_audit(b,a.plan_sha256);return
 if a.action=='audit-local':audit_local(b,a.plan_sha256);return
 if a.action=='snapshot-controller':snapshot(b,'controller');return
 if a.action=='snapshot-gatewaylink':snapshot(b,'gatewaylink');return
 if a.action=='controller-health':out=health(b,'controller')
 elif a.action=='terminal-health':out=health(b,'terminal')
 elif a.action=='gatewaylink-health':out=health(b,'gatewaylink')
 elif a.action=='restart-gatewaylink':
  before=r.read_json(b/'before-gatewaylink.json');r.require(r.digest(P(before['path']).read_bytes())==before['plist_sha256'],'gatewaylink plist changed')
  subprocess.check_call(['/bin/launchctl','kickstart','-k',GATEWAYLINK]);return
 elif a.action=='quiescence':out=closed_probe(b)
 elif a.action=='regate':
  rr,_=remote();g.acquire(rr,r.read_json(b/'original-offers.json'),timeout=60);await_local_safety(b);return
 elif a.action=='stopped':out={'stopped':inspect(SERVICE) is None}
 elif a.action=='stop':
  if inspect(SERVICE):subprocess.check_call(['/bin/launchctl','bootout',SERVICE])
  return
 elif a.action=='restart':
  before=r.read_json(b/'before-terminal.json');v=inspect(SERVICE)
  r.require(v and v['pid']>0,'worker missing before restart')
  original=(b/'original-terminal.plist').read_bytes()
  r.require(P(before['path']).read_bytes()==original and plistlib.loads(original).get('KeepAlive') is True,'restart policy changed')
  identity=subprocess.check_output(['/bin/ps','-p',str(v['pid']),'-o','uid=,lstart=,command=']).decode().strip()
  r.require(identity.split()[0]=='501' and str(TERM/'mac_provider.py') in identity,'worker PID identity differs')
  r.require(os.getuid()==501,'restart must use worker owner')
  proof=local_safety(b)
  r.require(restart_ready(b,proof),'local terminal execution is not restart-safe')
  r.dump(b/'independent-services-before.json',proof.get('independent_services',{}))
  tree=verified_worker_tree(v['pid'])
  # Only the verified worker child is signalled first when caffeinate owns it.
  # With Darwin's opposite topology the root is the worker; its watchdog exits
  # naturally and must never be mistaken for the worker signal target.
  if tree['worker']['pid']!=v['pid']:
   worker=tree['worker']
   current=subprocess.check_output(['/bin/ps','-p',str(worker['pid']),'-o','uid=,lstart=,command=']).decode().split(None,6)
   r.require(len(current)==7 and current[0]=='501' and ' '.join(current[1:6])==worker['started'] and current[6].strip()==worker['command'],'worker child PID identity changed before termination')
   current=subprocess.check_output(['/bin/ps','-p',str(v['pid']),'-o','uid=,lstart=,command=']).decode().strip()
   r.require(current==identity,'PID identity changed before child termination')
   os.kill(worker['pid'],signal.SIGTERM)
   end=time.monotonic()+10
   while time.monotonic()<end:
    current=inspect(SERVICE)
    if not current or current['pid']!=v['pid']:
     r.dump(b/'restarted-terminal.json',{'time':time.time(),'prior_pid':v['pid']});return
    time.sleep(.2)
  current=subprocess.check_output(['/bin/ps','-p',str(v['pid']),'-o','uid=,lstart=,command=']).decode().strip()
  r.require(current==identity,'PID identity changed before termination')
  os.kill(v['pid'],signal.SIGTERM)
  end=time.monotonic()+30
  while time.monotonic()<end:
   current=inspect(SERVICE)
   if not current or current['pid']!=v['pid']:
    r.dump(b/'restarted-terminal.json',{'time':time.time(),'prior_pid':v['pid']});return
   time.sleep(.2)
  raise RuntimeError('Prior worker PID did not exit')
 elif a.action=='start':
  r.require(inspect(SERVICE) is None,'worker already loaded')
  before=r.read_json(b/'before-terminal.json');path=P(before['path'])
  r.require(path.read_bytes()==(b/'original-terminal.plist').read_bytes(),'original launchd plist changed')
  subprocess.check_call(['/bin/launchctl','bootstrap','system',str(path)]);return
 else:raise RuntimeError('Unknown action')
 print(json.dumps(out))
if __name__=='__main__':main()
