#!/bin/bash
set -e
/usr/bin/python3 -S - 'IiIiSW5kZXBlbmRlbnQgb3duZXIgbWFpbnRlbmFuY2UgYWdlbnQuIE5vIGltcG9ydHMgb3IgY2FsbHMgaW50byBCaW9tZVJURS4iIiIKaW1wb3J0IGhhc2hsaWIKaW1wb3J0IGpzb24KaW1wb3J0IG9zCmZyb20gcGF0aGxpYiBpbXBvcnQgUGF0aAppbXBvcnQgc3FsaXRlMwppbXBvcnQgc3VicHJvY2VzcwppbXBvcnQgc3lzCmltcG9ydCB0aHJlYWRpbmcKaW1wb3J0IHRpbWUKZnJvbSB1cmxsaWIucmVxdWVzdCBpbXBvcnQgUmVxdWVzdCwgdXJsb3BlbgoKb3MudW1hc2soMG8wNzcpCkhPTUUgPSBQYXRoKHN5cy5hcmd2WzFdKQpDT05GID0ganNvbi5sb2FkcygoSE9NRSAvICdjb25maWcuanNvbicpLnJlYWRfdGV4dCgpKQpLRVkgPSAoSE9NRSAvICdkZXZpY2Uua2V5JykucmVhZF90ZXh0KCkuc3RyaXAoKQpEQiA9IHNxbGl0ZTMuY29ubmVjdChzdHIoSE9NRSAvICdsZWRnZXIuc3FsaXRlJyksIGNoZWNrX3NhbWVfdGhyZWFkPUZhbHNlKQpEQi5leGVjdXRlKCdQUkFHTUEgam91cm5hbF9tb2RlPVdBTCcpCkRCLmV4ZWN1dGUoJ0NSRUFURSBUQUJMRSBJRiBOT1QgRVhJU1RTIGNvbW1hbmRzKGlkIFRFWFQgUFJJTUFSWSBLRVksc3RhdGUgVEVYVCxyZXN1bHQgVEVYVCknKQpEQi5leGVjdXRlKCJVUERBVEUgY29tbWFuZHMgU0VUIHN0YXRlPSd1bmNlcnRhaW4nLHJlc3VsdD0/IFdIRVJFIHN0YXRlPSdydW5uaW5nJyIsIChqc29uLmR1bXBzKHsnc3RhdGUnOid1bmNlcnRhaW4nLCdyZWFzb24nOidNYWludGVuYW5jZSBhZ2VudCByZXN0YXJ0ZWQ7IHJlY29uY2lsZSBwcm9jZXNzIGFuZCBvdXRwdXQgZmlsZXMgYmVmb3JlIGFub3RoZXIgZXhlY3V0aW9uLid9KSwpKQpEQi5jb21taXQoKQpMT0NLID0gdGhyZWFkaW5nLlJMb2NrKCkKCgojIEpvYnMgYXJlIGFkbWl0dGVkIGJ5IHRoZSBhdXRoZW50aWNhdGVkIG1haW50ZW5hbmNlIGdhdGV3YXkuIFRoZSBkZXZpY2UKIyBjcmVkZW50aWFsIGFuZCBUTFMgdHJhbnNwb3J0IHJlbWFpbiByZXF1aXJlZDsgbm8gc2VwYXJhdGUgcGhvbmUgc2lnbmF0dXJlLgoKZGVmIGV4Y2hhbmdlKGJvZHkpOgogICAgcmVxdWVzdCA9IFJlcXVlc3QoQ09ORlsnb3JpZ2luJ10gKyAnL21haW50ZW5hbmNlL2RldmljZScsIGpzb24uZHVtcHMoYm9keSkuZW5jb2RlKCksCiAgICAgICAgICAgICAgICAgICAgICB7J0F1dGhvcml6YXRpb24nOidCZWFyZXIgJyArIEtFWSwgJ0NvbnRlbnQtVHlwZSc6J2FwcGxpY2F0aW9uL2pzb24nLAogICAgICAgICAgICAgICAgICAgICAgICdVc2VyLUFnZW50JzonQmlvbWVSVEUtSW5kZXBlbmRlbnQtTWFpbnRlbmFuY2UvMSd9KQogICAgd2l0aCB1cmxvcGVuKHJlcXVlc3QpIGFzIHJlc3BvbnNlOgogICAgICAgIHJldHVybiBqc29uLmxvYWRzKHJlc3BvbnNlLnJlYWQoKSkKCgpkZWYgZXhlY3V0ZShqb2IpOgogICAgZGlyZWN0b3J5ID0gSE9NRSAvICdjb21tYW5kcycgLyBoYXNobGliLnNoYTI1Nihqb2JbJ2lkJ10uZW5jb2RlKCkpLmhleGRpZ2VzdCgpCiAgICB0cnk6CiAgICAgICAgZGlyZWN0b3J5Lm1rZGlyKHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSkKICAgICAgICB3aXRoIChkaXJlY3RvcnkvJ3N0ZG91dCcpLm9wZW4oJ3diJykgYXMgc3Rkb3V0LCAoZGlyZWN0b3J5LydzdGRlcnInKS5vcGVuKCd3YicpIGFzIHN0ZGVycjoKICAgICAgICAgICAgcHJvY2VzcyA9IHN1YnByb2Nlc3MuUG9wZW4oam9iWydhcmd2J10sIGN3ZD1qb2IuZ2V0KCdjd2QnKSBvciBOb25lLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBzdGRvdXQ9c3Rkb3V0LCBzdGRlcnI9c3RkZXJyLCBzdGFydF9uZXdfc2Vzc2lvbj1UcnVlKQogICAgICAgICAgICAoZGlyZWN0b3J5LydwaWQnKS53cml0ZV90ZXh0KHN0cihwcm9jZXNzLnBpZCkpCiAgICAgICAgICAgIGNvZGUgPSBwcm9jZXNzLndhaXQoKQogICAgICAgIHJlc3VsdCA9IHsnc3RhdGUnOidzdWNjZWVkZWQnIGlmIGNvZGUgPT0gMCBlbHNlICdmYWlsZWQnLCAnZXhpdF9jb2RlJzpjb2RlLAogICAgICAgICAgICAgICAgICAnc3Rkb3V0JzooZGlyZWN0b3J5LydzdGRvdXQnKS5yZWFkX3RleHQoZXJyb3JzPSdyZXBsYWNlJyksCiAgICAgICAgICAgICAgICAgICdzdGRlcnInOihkaXJlY3RvcnkvJ3N0ZGVycicpLnJlYWRfdGV4dChlcnJvcnM9J3JlcGxhY2UnKSwKICAgICAgICAgICAgICAgICAgJ291dHB1dF9kaXJlY3RvcnknOnN0cihkaXJlY3RvcnkpfQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlcnJvcjoKICAgICAgICByZXN1bHQgPSB7J3N0YXRlJzonZmFpbGVkJywnZXJyb3InOnR5cGUoZXJyb3IpLl9fbmFtZV9fICsgJzogJyArIHN0cihlcnJvciksCiAgICAgICAgICAgICAgICAgICdvdXRwdXRfZGlyZWN0b3J5JzpzdHIoZGlyZWN0b3J5KX0KICAgIHNhdmVfcmVzdWx0KGpvYlsnaWQnXSwgcmVzdWx0KQoKCmRlZiBzYXZlX3Jlc3VsdChpZGVudGlmaWVyLCByZXN1bHQpOgogICAgd2l0aCBMT0NLOgogICAgICAgIERCLmV4ZWN1dGUoJ1VQREFURSBjb21tYW5kcyBTRVQgc3RhdGU9PyxyZXN1bHQ9PyBXSEVSRSBpZD0/JywKICAgICAgICAgICAgICAgICAgIChyZXN1bHRbJ3N0YXRlJ10sanNvbi5kdW1wcyhyZXN1bHQpLGlkZW50aWZpZXIpKQogICAgICAgIERCLmNvbW1pdCgpCgoKZGVmIHN0YXJ0X2pvYihqb2IpOgogICAgd2l0aCBMT0NLOgogICAgICAgIGtub3duID0gREIuZXhlY3V0ZSgnU0VMRUNUIHN0YXRlIEZST00gY29tbWFuZHMgV0hFUkUgaWQ9PycsKGpvYlsnaWQnXSwpKS5mZXRjaG9uZSgpCiAgICAgICAgaWYga25vd246CiAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgIERCLmV4ZWN1dGUoIklOU0VSVCBJTlRPIGNvbW1hbmRzIFZBTFVFUyg/LCdydW5uaW5nJyxOVUxMKSIsKGpvYlsnaWQnXSwpKQogICAgICAgIERCLmNvbW1pdCgpCiAgICB0cnk6CiAgICAgICAgdGhyZWFkaW5nLlRocmVhZCh0YXJnZXQ9ZXhlY3V0ZSxhcmdzPShqb2IsKSxkYWVtb249VHJ1ZSkuc3RhcnQoKQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlcnJvcjoKICAgICAgICBzYXZlX3Jlc3VsdChqb2JbJ2lkJ10sIHsnc3RhdGUnOidmYWlsZWQnLAogICAgICAgICAgICAgICAgICAgICdlcnJvcic6J01haW50ZW5hbmNlIHdvcmtlciBjb3VsZCBub3Qgc3RhcnQ6ICcgKyB0eXBlKGVycm9yKS5fX25hbWVfX30pCiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICByZXR1cm4gVHJ1ZQoKCndoaWxlIFRydWU6CiAgICB0cnk6CiAgICAgICAgd2l0aCBMT0NLOgogICAgICAgICAgICBjb21wbGV0ZWQgPSBsaXN0KERCLmV4ZWN1dGUoIlNFTEVDVCBpZCxyZXN1bHQgRlJPTSBjb21tYW5kcyBXSEVSRSBzdGF0ZSBJTiAoJ3N1Y2NlZWRlZCcsJ2ZhaWxlZCcsJ3VuY2VydGFpbicpIikpCiAgICAgICAgZm9yIGlkZW50aWZpZXIsIHJlc3VsdCBpbiBjb21wbGV0ZWQ6CiAgICAgICAgICAgIGV4Y2hhbmdlKHsnb3BlcmF0aW9uJzonY29tcGxldGUnLCdpZCc6aWRlbnRpZmllciwncmVzdWx0Jzpqc29uLmxvYWRzKHJlc3VsdCl9KQogICAgICAgICAgICB3aXRoIExPQ0s6CiAgICAgICAgICAgICAgICBEQi5leGVjdXRlKCJVUERBVEUgY29tbWFuZHMgU0VUIHN0YXRlPSdyZXBvcnRlZCcgV0hFUkUgaWQ9PyIsKGlkZW50aWZpZXIsKSkKICAgICAgICAgICAgICAgIERCLmNvbW1pdCgpCiAgICAgICAgam9icyA9IGV4Y2hhbmdlKHsnb3BlcmF0aW9uJzoncG9sbCd9KVsnam9icyddCiAgICAgICAgZm9yIGpvYiBpbiBqb2JzOgogICAgICAgICAgICBzdGFydF9qb2Ioam9iKQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlcnJvcjoKICAgICAgICAjIE5ldmVyIHByaW50IGNyZWRlbnRpYWxzLCByZXF1ZXN0IGJvZGllcyBvciBjb21tYW5kIG91dHB1dCB0byBsYXVuY2hkIGxvZ3MuCiAgICAgICAgcHJpbnQoJ21haW50ZW5hbmNlIHRyYW5zcG9ydCByZXRyeTogJyt0eXBlKGVycm9yKS5fX25hbWVfXyxmbHVzaD1UcnVlKQogICAgdGltZS5zbGVlcCgxKQo=' <<'BIOME_MAINTENANCE_INSTALLER'
import base64,hashlib,json,os,pathlib,plistlib,secrets,sqlite3,subprocess,sys,time,uuid
os.umask(0o077)
control=plistlib.loads((pathlib.Path.home()/'Library/LaunchAgents/org.biomerte.controller.plist').read_bytes())
args=control['ProgramArguments'];data=pathlib.Path(args[args.index('--home')+1]) if '--home' in args else pathlib.Path.home()/'.local/share/biomerte'
root=pathlib.Path(control['WorkingDirectory']);sys.path.insert(0,str(root))
from biomerte.app import Application
from biomerte import providers
def cancel_superseded(tx):
 for name in ('install-independent-maintenance-20261105','install-confirmed-maintenance-20261105'):
  old=tx.get('actions',name)
  if old and old['state']=='claimed':raise RuntimeError('A remote installer is executing; wait for it to finish.')
  if old and old['state']=='queued':
   old.update(state='failed',result={'cancelled_before_execution':True,'reason':'Replaced by local approved-channel installation'})
   tx.put('actions',name,old);providers.release(tx,tx.need('leases',old['lease']))
Application(str(data)).stores['site'].execute(cancel_superseded)
node=json.loads((data/'node.json').read_text())
with sqlite3.connect(str(data/('gateway-'+node['site']+'.sqlite'))) as db:
 phones=[json.loads(row[0]) for row in db.execute("SELECT value FROM records WHERE kind='_decision_devices' ORDER BY id") if json.loads(row[0]).get('enabled')]
if not phones:raise RuntimeError('Enroll your Android Decisions app first.')
phone=phones[0];home=pathlib.Path.home()/'.local/share/biomerte-maintenance';home.mkdir(parents=True,exist_ok=True)
agent=base64.b64decode(sys.argv[1]);compile(agent,'maintenance_agent.py','exec');(home/'agent.py').write_bytes(agent)
key=home/'device.key'
if not key.exists():key.write_text(secrets.token_hex(32))
key.chmod(0o600)
(home/'config.json').write_text(json.dumps({'origin':'https://biomerte-controller.cantikash.chatgpt.site','host':'mac-maintenance','site':node['site'],'phone_device':phone['id'],'phone_key':phone['key']}))
f=root/'biomerte/decisions.py';s=f.read_text()
if 'def maintenance_identity(' not in s:
 s+="\n\n@op('decision.maintenance_identity', write=False)\ndef maintenance_identity(tx, ctx):\n    operator(ctx)\n    devices = [d for d in tx.rows('_decision_devices') if d.get('enabled')]\n    if not devices: raise Fault('Enrolled Android decision device required', 409)\n    device = devices[0]\n    return {'device': device['id'], 'key': device['key'], 'site': ctx.app.site}\n"
 compile(s,str(f),'exec');f.with_name(f.name+'.before-maintenance-identity').write_bytes(f.read_bytes());f.write_text(s)
 subprocess.run(['launchctl','kickstart','-k','gui/'+str(os.getuid())+'/org.biomerte.controller'],check=True)
sys.path.insert(0,str(pathlib.Path.home()/'.local/share/biome-mac-terminal'))
from mac_provider import invoke
while True:
 try:
  invoke('decision.maintenance_identity',{},'maint-identity-'+uuid.uuid4().hex);break
 except Exception:
  print('Waiting for the normal controller to verify the enrolled Android identity...',flush=True);time.sleep(2)
invoke('maintenance.enroll',{'host':'mac-maintenance','key_hash':hashlib.sha256(key.read_text().strip().encode()).hexdigest()},'maintenance-enroll-'+hashlib.sha256(key.read_bytes()).hexdigest()[:24])
label='org.biomerte.maintenance';domain='gui/'+str(os.getuid());p=pathlib.Path.home()/'Library/LaunchAgents'/(label+'.plist')
p.write_bytes(plistlib.dumps({'Label':label,'ProgramArguments':[args[0],'-S',str(home/'agent.py'),str(home)],'WorkingDirectory':str(home),'RunAtLoad':True,'KeepAlive':True,'StandardOutPath':str(home/'service.log'),'StandardErrorPath':str(home/'service-error.log')}))
subprocess.run(['launchctl','bootout',domain+'/'+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
subprocess.run(['launchctl','bootstrap',domain,str(p)],check=True)
print('Installed independent owner-authenticated maintenance. Command IDs remain protected by the local ledger.')

BIOME_MAINTENANCE_INSTALLER
