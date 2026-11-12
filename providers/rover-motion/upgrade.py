#!/usr/bin/env python3
"""Upgrade only the dedicated motion worker, retaining its identity and ledger."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import time


def bootstrap(domain, plist):
    # launchd can acknowledge bootout before allowing the same label back in.
    deadline=time.monotonic()+15.
    while True:
        result=subprocess.run(["launchctl","bootstrap",domain,str(plist)],capture_output=True,text=True)
        if result.returncode==0:return
        observed=subprocess.run(["launchctl","print",domain+"/org.biomerte.rover-motion-provider"],capture_output=True,text=True)
        expected=plistlib.loads(plist.read_bytes())["ProgramArguments"][1]
        if observed.returncode==0 and expected in observed.stdout:return
        if result.returncode!=5 or observed.returncode==0 or time.monotonic()>=deadline:
            raise subprocess.CalledProcessError(result.returncode,result.args,output=result.stdout,stderr=result.stderr)
        time.sleep(.25)


def upgrade(source_root, state):
    state=Path(state).resolve();source_root=Path(source_root).resolve()
    config_file=state/"provider.json"
    original_config=config_file.read_bytes()
    cfg=json.loads(original_config)
    label="org.biomerte.rover-motion-provider"
    plist=Path.home()/"Library/LaunchAgents"/(label+".plist")
    original_plist=plist.read_bytes()
    launch=plistlib.loads(original_plist)
    paths=["biomerte/motion_model.py","biomerte/motion_planning.py",
           "providers/rover-motion/biome_provider.py"]
    payload={p:(source_root/p).read_bytes() for p in paths}
    identity=hashlib.sha256(b"".join(p.encode()+payload[p] for p in paths)).hexdigest()
    release=state.parent/"provider-releases"/identity[:16]
    runtime=release/"runtime"
    package=runtime/"biomerte";package.mkdir(parents=True,exist_ok=True)
    # Only motion modules change. All existing runtime dependencies stay pinned.
    base_runtime=cfg.get("base_model_runtime",cfg["runtime"])
    (package/"__init__.py").write_text("__path__.append("+repr(str(Path(base_runtime)/"biomerte"))+")\n")
    for path in paths[:2]:(package/Path(path).name).write_bytes(payload[path])
    adapter=release/"biome_provider.py";adapter.write_bytes(payload[paths[2]])
    env=dict(os.environ,PYTHONPATH=str(runtime))
    subprocess.run([cfg["python"],"-c",
        "import importlib.util; from biomerte.motion_planning import plan_motion; "
        "from biomerte.motion_model import MotionModel; "
        "assert plan_motion(MotionModel(),dict(distance_m=.1,yaw_deg=0.))['status']=='insufficient_calibration'"],
        env=env,check=True)

    def idle():
        with sqlite3.connect((state/"worker.sqlite").as_uri()+"?mode=ro",uri=True) as db:
            active=db.execute("SELECT count(*) FROM actions WHERE state NOT IN ('succeeded','failed','abandoned')").fetchone()[0]
            pending=db.execute("SELECT count(*) FROM supervised_pending").fetchone()[0]
            if active or pending:raise RuntimeError("Motion worker has unsettled work; reconcile before upgrade")
    idle()
    service="gui/"+str(os.getuid())+"/"+label
    domain="gui/"+str(os.getuid())
    subprocess.run(["launchctl","bootout",service],check=True)
    try:
        deadline=time.monotonic()+30.
        with (state/"worker.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise RuntimeError("Old motion worker is still shutting down")
                    time.sleep(.25)
        idle()
        cfg.update(runtime=str(runtime),base_model_runtime=base_runtime,adapter_path=str(adapter),
                   motion_source_sha256=identity)
        launch["ProgramArguments"]=[cfg["python"],str(adapter),"serve","--state",str(state)]
        launch["WorkingDirectory"]=str(release)
        launch.setdefault("EnvironmentVariables",{})["PYTHONPATH"]=str(runtime)
        config_file.write_text(json.dumps(cfg,indent=2))
        plist.write_bytes(plistlib.dumps(launch))
        bootstrap(domain,plist)
    except Exception:
        config_file.write_bytes(original_config);plist.write_bytes(original_plist)
        bootstrap(domain,plist)
        raise
    print(json.dumps(dict(upgraded=True,provider=cfg["provider"],resource=cfg["resource"],source_sha256=identity)))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root",required=True)
    parser.add_argument("--state",required=True)
    args=parser.parse_args()
    upgrade(args.source_root,args.state)
