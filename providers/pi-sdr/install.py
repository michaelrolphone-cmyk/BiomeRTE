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
    p.add_argument('--esp-serial', help='Explicitly commissioned ESP-SDR USB serial; never probes other boards')
    p.add_argument('--esp-alias', default=None)
    a=p.parse_args()
    if a.esp_serial:
        import routing
        os.makedirs(a.state, exist_ok=True)
        path=os.path.join(a.state,'receivers.json')
        try:
            with open(path) as f: receivers=json.load(f)
        except FileNotFoundError: receivers={'revision':0,'receivers':{}}
        identity='esp:'+routing.normalize_serial(a.esp_serial)
        receivers['receivers'].setdefault(identity, {'driver':'esp_sdr','serial':a.esp_serial,
            'aliases':[a.esp_alias] if a.esp_alias else [], 'label':a.esp_alias or identity})
        receivers['revision']+=1
        tmp=path+'.tmp'
        with open(tmp,'w') as f: json.dump(receivers,f,sort_keys=True)
        os.replace(tmp,path)
    home=os.path.abspath(a.home); runtime=os.path.abspath(a.runtime)
    source=os.path.dirname(os.path.abspath(__file__))
    os.makedirs(home,exist_ok=True)
    sys.path.insert(0,runtime)
    from biomerte.app import initialize
    conf=initialize(home,a.site)
    biome=os.path.join(runtime,'biome')
    if a.phase=='prepare':
        subprocess.check_call([biome,'--home',home,'pair',a.url,'--label','Receive-only SDR provider'])
        print(json.dumps({'required_scopes':['cap.offer','cap.status','cap.fenced','cap.configured','provider.claim','provider.result','provider.poll','offer:radio.sdr.receive:1','offer:radio.sdr.receive:2','resource:radio.sdr.'+conf['node'][:24]]}))
        return
    if not os.path.exists(os.path.join(home,'manifest.json')):
        subprocess.check_call([biome,'--home',home,'pair',a.url,'--finish'])
    usage={'before':'Receive-only SDR. Discover devices first; scan/sample return scan_id. Poll results. Monitor changes use expected_revision from status.',
           'example_inputs':{'action':'devices'},'read_result':'Check result.ok and result.data; hardware and validation failures return result.error.',
           'parameters':{'action':'devices, receivers, receiver_configure, receiver_reset_antenna, scan, sample, results, status, monitor_start, monitor_stop, events or mark',
                         'device':'Omit or auto: choose by complete frequency window and configured antenna coverage. Or stable receiver id, numeric RTL index, unique USB serial, or configured ESP alias.',
                         'frequency_hz':'RTL: 25 MHz-1.75 GHz; ESP-S3: vendor characterized 1.85-2.75 GHz. Full window must fit. Use devices for effective antenna bands.',
                         'antenna_ranges_hz':'receiver_configure with device, expected_revision from receivers, and [[low_hz,high_hz],...]. [] excludes auto selection; null resets to hardware coverage.',
                         'gain_index':'ESP PHY table index 0..82; omit for hardware AGC. gain_db and ppm are RTL-only.',
                         'scan_id':'Use scan_id and offset; one spectrum window per page. IQ is cu8 base64 when include_iq=true.'}}
    manifest={'url':a.url,'site':a.site,'timeout':None,'poll_seconds':2,'parallelism':5,
              'offers':[{'contract':'radio.sdr.receive:1','resource':'radio.sdr.'+conf['node'][:24],
                         'service':'sdr-receiver','capacity':5,'configuration':{'host':__import__('socket').gethostname()},
                         'argv':[sys.executable,os.path.join(source,'server.py'),'call',home],'usage':usage}]}
    # Both versions share the same physical resource, locks and daemon. Keep v1
    # usable while v2 adds antenna/gain configuration fields to its immutable ABI.
    import copy
    extended=copy.deepcopy(manifest['offers'][0])
    extended['contract']='radio.sdr.receive:2'
    manifest['offers'].insert(0,extended)
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
    print(json.dumps({'provider':conf['node'],'contract':'radio.sdr.receive:2','compatibility_contract':'radio.sdr.receive:1','manifest':path}))

if __name__=='__main__': main()
