"""Install the additive Mac transform API and provider from this source tree."""
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from .provider import ROOT,FIELD_ROOT,configure


def main():
    source=Path(__file__).resolve().parents[2]
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    staging=Path(tempfile.mkdtemp(prefix='source-pending-',dir=ROOT))
    try:
        shutil.copytree(source,staging,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','*.egg-info','.venv'))
        hashes={str(p.relative_to(staging)):hashlib.sha256(p.read_bytes()).hexdigest() for p in staging.rglob('*.py')}
        source_hash=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
        dest=ROOT/('source-'+source_hash)
        if not dest.exists():os.replace(staging,dest)
        revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
        receipt=dict(git_revision=revision,source_hash=source_hash,source=str(dest),capabilities=configure())
        agents=Path.home()/'Library/LaunchAgents';agents.mkdir(parents=True,exist_ok=True)
        for suffix,module,args in [('api','activation_field.transforms.server',[]),('worker','activation_field.transforms.provider',['worker'])]:
            label='org.biomerte.field-transform-'+suffix
            plist=dict(Label=label,ProgramArguments=[sys.executable,'-m',module,*args],RunAtLoad=True,KeepAlive=True,
                       WorkingDirectory=str(dest),EnvironmentVariables=dict(PYTHONPATH=str(dest),PYTHONUNBUFFERED='1'),
                       StandardOutPath=str(ROOT/(suffix+'.log')),StandardErrorPath=str(ROOT/(suffix+'-error.log')))
            path=agents/(label+'.plist');path.write_bytes(plistlib.dumps(plist));path.chmod(0o600)
            domain='gui/'+str(os.getuid());service=domain+'/'+label
            found=subprocess.run(['launchctl','print',service],capture_output=True).returncode==0
            if found:
                subprocess.run(['launchctl','bootout',service],check=True)
                # bootout can acknowledge before launchd removes the old job.
                # Wait for that actual state transition, not an arbitrary delay.
                while subprocess.run(['launchctl','print',service],capture_output=True).returncode==0:
                    time.sleep(.1)
            subprocess.run(['launchctl','bootstrap',domain,str(path)],check=True)
        (ROOT/'source-version.json').write_text(json.dumps(receipt,indent=2))
        print(json.dumps(receipt))
    finally:
        if staging.exists():shutil.rmtree(staging)


if __name__=='__main__':main()
