#!/usr/bin/env python3
"""Install a standalone SDR daemon and BiomeRTE provider on Linux."""
import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
import pwd

def main():
    p=argparse.ArgumentParser()
    p.add_argument('phase',choices=['prepare','activate'])
    p.add_argument('--runtime',required=True)
    p.add_argument('--url',required=True)
    p.add_argument('--site',required=True)
    p.add_argument('--home',default=os.path.join(pwd.getpwuid(os.getuid()).pw_dir,'.local/share/biomerte-sdr'))
    p.add_argument('--state',default=os.path.join(pwd.getpwuid(os.getuid()).pw_dir,'.local/share/sdr-receiver'))
    a=p.parse_args()
    home=os.path.abspath(a.home); runtime=os.path.abspath(a.runtime)
    source=os.path.dirname(os.path.abspath(__file__))
    os.makedirs(home,exist_ok=True)
    sys.path.insert(0,runtime)
    from biomerte.app import initialize
    conf=initialize(home,a.site)
    biome=os.path.join(runtime,'biome')
    if a.phase=='prepare':
        subprocess.check_call([biome,'--home',home,'pair',a.url,'--label','Receive-only SDR provider'])
        print(json.dumps({'required_scopes':['cap.offer','cap.status','cap.fenced','cap.configured','provider.claim','provider.result','provider.poll','offer:radio.sdr.receive:1','resource:radio.sdr.'+conf['node'][:24]]}))
        return
    if not os.path.exists(os.path.join(home,'manifest.json')):
        subprocess.check_call([biome,'--home',home,'pair',a.url,'--finish'])
    usage={'before':'Receive-only SDR. Discover devices first; scan/sample return scan_id. Poll results. Monitor changes use expected_revision from status.',
           'example_inputs':{'action':'devices'},'read_result':'Check result.ok and result.data; hardware and validation failures return result.error.',
           'parameters':{'action':'devices, scan, sample, results, status, monitor_start, monitor_stop, events or mark',
                         'device':'Numeric index or exact unique USB serial from devices',
                         'frequency_hz':'25 MHz to 1.75 GHz; alternatively frequencies_hz or profile=garage_us',
                         'scan_id':'Use scan_id and offset; one spectrum window per page. IQ is cu8 base64 when include_iq=true.'}}
    manifest={'url':a.url,'site':a.site,'timeout':None,'poll_seconds':2,'parallelism':4,
              'offers':[{'contract':'radio.sdr.receive:1','resource':'radio.sdr.'+conf['node'][:24],
                         'service':'sdr-receiver','capacity':4,'configuration':{'host':__import__('socket').gethostname()},
                         'argv':[sys.executable,os.path.join(source,'server.py'),'call',home],'usage':usage}]}
    path=os.path.join(home,'manifest.json')
    with open(path,'w') as f: json.dump(manifest,f,indent=2)
    units={
      'biomerte-sdr.service':('Persistent receive-only SDR service',[sys.executable,os.path.join(source,'server.py'),'serve',home,a.state],''),
      'biomerte-sdr-provider.service':('BiomeRTE SDR capability provider',[biome,'--home',home,'worker',path],'Requires=biomerte-sdr.service\nAfter=biomerte-sdr.service\n')}
    def quote(s): return '"'+s.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
    for name,(description,argv,extra) in units.items():
        text='[Unit]\nDescription='+description+'\nAfter=biomerte.service\n'+extra+'[Service]\nUser='+getpass.getuser()+'\nUMask=0077\nExecStart=/usr/bin/env '+' '.join(quote(x) for x in argv)+'\nRestart=always\nRestartSec=3\n[Install]\nWantedBy=multi-user.target\n'
        with tempfile.NamedTemporaryFile() as f:
            f.write(text.encode()); f.flush()
            subprocess.check_call(['sudo','install','-m','0644',f.name,'/etc/systemd/system/'+name])
    subprocess.check_call(['sudo','systemctl','daemon-reload'])
    subprocess.check_call(['sudo','systemctl','enable']+list(units))
    subprocess.check_call(['sudo','systemctl','start']+list(units))
    print(json.dumps({'provider':conf['node'],'contract':'radio.sdr.receive:1','manifest':path}))

if __name__=='__main__': main()
