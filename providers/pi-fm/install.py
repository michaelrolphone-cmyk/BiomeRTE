#!/usr/bin/env python3
"""Install the standalone FM daemon and enrolled BiomeRTE provider."""
import argparse
import json
import os
import pwd
import socket
import subprocess
import tempfile

def install(runtime, source, home, root, url, site):
    biome = os.path.join(runtime, 'biome')
    subprocess.check_call([biome,'--home',home,'pair',url,'--finish'])
    usage = {'before':'One physical GPIO4 FM transmitter. capabilities/status/stop do not transmit. Transmit needs explicit frequency_hz and duration_seconds; concurrent transmissions return busy. Jobs never auto-resume.',
             'example_inputs':{'action':'capabilities'},
             'read_result':'Check result.ok, then result.data.job_id. Poll status for terminal state and cleanup.verified; stop uses job_id.',
             'parameters':{'action':'capabilities|transmit|status|stop','frequency_hz':'88100000..107900000','duration_seconds':'1..120','source':'tone (default) or wav','request_id':'Optional durable transmission ID; defaults to SHA256 of Biome operation ID.'}}
    manifest = {'url':url,'site':site,'timeout':None,
                'poll_seconds':0.1,'parallelism':4,'offers':[{'contract':'radio.fm.transmit:1',
                'resource':'radio.fm.gpio4.'+socket.gethostname().lower(),'service':'fm-transmitter','capacity':4,
                'configuration':{'execution_host':socket.gethostname()},
                'argv':['/usr/bin/python3',os.path.join(source,'server.py'),'call',home],'usage':usage}]}
    path = os.path.join(home,'manifest.json')
    with open(path,'w') as out: json.dump(manifest,out,indent=2)
    units = {
      'biomerte-fm.service':('Persistent FM broadcast daemon',['/usr/bin/python3',os.path.join(source,'server.py'),'serve',home,root],''),
      'biomerte-fm-provider.service':('BiomeRTE FM broadcast capability provider',[biome,'--home',home,'worker',path],'Requires=biomerte-fm.service\nAfter=biomerte-fm.service\n')}
    def quote(s):
        return '"'+s.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
    for name,(description,argv,extra) in units.items():
        content='[Unit]\nDescription='+description+'\nAfter=biomerte.service\n'+extra+'[Service]\nUser=pi\nUMask=0077\nExecStart=/usr/bin/env '+' '.join(quote(x) for x in argv)+'\nRestart=always\nRestartSec=3\n[Install]\nWantedBy=multi-user.target\n'
        with tempfile.NamedTemporaryFile() as out:
            out.write(content.encode()); out.flush()
            subprocess.check_call(['sudo','install','-m','0644',out.name,'/etc/systemd/system/'+name])
    subprocess.check_call(['sudo','systemctl','daemon-reload'])
    subprocess.check_call(['sudo','systemctl','enable']+list(units))
    subprocess.check_call(['sudo','systemctl','start']+list(units))
    print(json.dumps({'contract':'radio.fm.transmit:1','resource':manifest['offers'][0]['resource'],'manifest':path}))

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--runtime',required=True)
    p.add_argument('--home',default=os.path.join(pwd.getpwuid(os.getuid()).pw_dir,'.local/share/biomerte-fm'))
    p.add_argument('--root',default=os.path.join(pwd.getpwuid(os.getuid()).pw_dir,'.local/share/fm-transmitter'))
    p.add_argument('--url',default='http://127.0.0.1:8740')
    p.add_argument('--site',required=True)
    args=p.parse_args()
    install(os.path.abspath(args.runtime),os.path.dirname(os.path.abspath(__file__)),os.path.abspath(args.home),os.path.abspath(args.root),args.url,args.site)
