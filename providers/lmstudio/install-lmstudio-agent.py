"""Install the BiomeRTE agent for the signed-in macOS user."""
import os
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
source = Path(__file__).with_name(target.name)
if source.resolve() != target.resolve():
    shutil.copyfile(source, target.with_suffix('.new'))
    os.replace(target.with_suffix('.new'), target)
label='org.biomerte.agent.lmstudio'
plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
plist.parent.mkdir(parents=True, exist_ok=True)
data={'Label':label,'ProgramArguments':[sys.executable,'-u',str(target)],
      'RunAtLoad':True,'KeepAlive':True,'WorkingDirectory':str(root),
      'StandardOutPath':str(root/'service.log'),'StandardErrorPath':str(root/'service.err')}
with plist.with_suffix('.new').open('wb') as f: plistlib.dump(data,f)
os.replace(plist.with_suffix('.new'),plist)
domain='gui/'+str(os.getuid())
subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=True)
print('Installed '+domain+'/'+label)
