#!/usr/bin/env python3
"""Install the dedicated native motion capability worker beside its observer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

def install(resource):
    home=Path.home()
    observer=plistlib.loads((home/"Library/LaunchAgents/org.biomerte.rover-motion.plist").read_bytes())
    vision=json.loads((home/".local/share/biome-robot-vision/provider-manifest.json").read_text())
    state=home/".local/share/biome-rover-motion/provider"
    state.mkdir(parents=True,exist_ok=True);state.chmod(0o700)
    config_file=state/"provider.json"
    previous=json.loads(config_file.read_text()) if config_file.exists() else None
    if previous:
        if previous["site"]!=vision["site"] or previous["resource"]!=resource:
            raise RuntimeError("Existing provider belongs to another robot/authority; use a separate state directory")
        provider=previous["provider"]
    else:provider=hashlib.sha256(("robot-motion:"+vision["site"]+":"+resource).encode()).hexdigest()
    source=Path(__file__).with_name("biome_provider.py")
    release=state.parent/"provider-releases"/hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    release.mkdir(parents=True,exist_ok=True)
    adapter=release/"biome_provider.py"
    shutil.copyfile(source,adapter)
    cfg=dict(provider=provider,site=vision["site"],resource=resource,slots=16,
        state=str(state),gateway_state=vision["gateway_state"],
        worker_runtime=str(Path(vision["gateway_state"])/"runtime"),
        runtime=observer["EnvironmentVariables"]["PYTHONPATH"],
        python=observer["ProgramArguments"][0],adapter_path=str(adapter),
        database=str(state.parent/"motion/observations.sqlite"))
    label="org.biomerte.rover-motion-provider"
    plist=home/"Library/LaunchAgents"/(label+".plist")
    current=subprocess.run(["launchctl","print","gui/"+str(os.getuid())+"/"+label],
                           capture_output=True,text=True)
    if current.returncode==0:
        raise RuntimeError("Provider already registered; reconcile active work before replacing its launch agent")
    config_file.write_text(json.dumps(cfg,indent=2))
    plist.write_bytes(plistlib.dumps(dict(Label=label,
        ProgramArguments=[cfg["python"],str(adapter),"serve","--state",str(state)],
        WorkingDirectory=str(release),EnvironmentVariables=dict(HOME=str(home),PYTHONPATH=cfg["runtime"]),
        RunAtLoad=True,KeepAlive=True,StandardOutPath=str(state/"provider.log"),
        StandardErrorPath=str(state/"provider-error.log"))))
    subprocess.run(["launchctl","bootstrap","gui/"+str(os.getuid()),str(plist)],check=True)
    print("Native motion provider installed:",provider,resource)

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource",required=True,help="Logical motion-model resource for the selected robot")
    args=parser.parse_args()
    install(args.resource)
