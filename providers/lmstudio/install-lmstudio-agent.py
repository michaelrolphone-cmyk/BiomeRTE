"""Install the BiomeRTE agent for the signed-in macOS user."""
import os
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

root = Path.home() / '.local/share/biome-lmstudio'
terminal = Path.home() / '.local/share/biome-mac-terminal'
if not (terminal/'mac_provider.py').is_file():
    raise SystemExit('Install and authorize the BiomeRTE Mac terminal transport first.')
os.umask(0o077)
root.mkdir(mode=0o700, parents=True, exist_ok=True)
target = root / 'lmstudio_agent.py'
for name in ['lmstudio_agent.py','context_recovery.py','context_memory.py','coding_mcp.py','coding_http.mjs']:
    source=Path(__file__).with_name(name)
    destination=root/name
    if source.is_file() and source.resolve()!=destination.resolve():
        shutil.copyfile(source,destination.with_suffix('.new'))
        os.replace(destination.with_suffix('.new'),destination)
cfg=root/'coding-config.json'
if cfg.exists():
    configuration=json.loads(cfg.read_text())
    Path(configuration['workspace']).resolve(strict=True)
    for required in ['api-token','mcp-token','coding_mcp.py','coding_http.mjs',
                     'coding-deps/node_modules/@modelcontextprotocol/server-filesystem/dist/server.js']:
        if not (root/required).is_file():raise SystemExit('Missing coding prerequisite: '+required)
label='org.biomerte.agent.lmstudio'
plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
plist.parent.mkdir(parents=True, exist_ok=True)
data={'Label':label,'ProgramArguments':[sys.executable,'-S','-u',str(target)],
      'RunAtLoad':True,'KeepAlive':True,'WorkingDirectory':str(root),
      'StandardOutPath':str(root/'service.log'),'StandardErrorPath':str(root/'service.err')}
with plist.with_suffix('.new').open('wb') as f: plistlib.dump(data,f)
os.replace(plist.with_suffix('.new'),plist)
domain='gui/'+str(os.getuid())
if cfg.exists():
    coding_label='org.biomerte.lmstudio-coding-mcp'
    coding_plist=plist.with_name(coding_label+'.plist')
    coding_data={'Label':coding_label,'ProgramArguments':['/opt/homebrew/bin/node',str(root/'coding_http.mjs')],
                 'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':True,
                 'StandardOutPath':str(root/'coding-mcp.log'),'StandardErrorPath':str(root/'coding-mcp.log'),
                 'EnvironmentVariables':{'HOME':str(Path.home()),'PATH':'/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'}}
    coding_plist.write_bytes(plistlib.dumps(coding_data))
    subprocess.run(['launchctl','bootout',domain+'/'+coding_label],capture_output=True)
    subprocess.run(['launchctl','bootstrap',domain,str(coding_plist)],check=True)
subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=True)
print('Installed '+domain+'/'+label)
