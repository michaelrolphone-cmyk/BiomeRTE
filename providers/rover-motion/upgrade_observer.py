#!/usr/bin/env python3
"""Replace only the passive recorder service, preserving the observation store."""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import plistlib
import subprocess


def upgrade(source_root):
    source_root=Path(source_root).resolve()
    helper_path=source_root/"providers/rover-motion/upgrade.py"
    spec=importlib.util.spec_from_file_location("motion_worker_upgrade",helper_path)
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    label="org.biomerte.rover-motion"
    plist=Path.home()/"Library/LaunchAgents"/(label+".plist")
    original=plist.read_bytes();launch=plistlib.loads(original)
    paths=["biomerte/motion_model.py","biomerte/motion_training.py","biomerte/motion_tracking.py","deploy/rover-motion-observer.py"]
    payload={p:(source_root/p).read_bytes() for p in paths}
    identity=hashlib.sha256(b"".join(p.encode()+payload[p] for p in paths)).hexdigest()
    release=Path.home()/".local/share/biome-rover-motion/observer-releases"/identity[:16]
    package=release/"biomerte";package.mkdir(parents=True,exist_ok=True)
    environment=launch.setdefault("EnvironmentVariables",{})
    base=environment.get("BIOME_OBSERVER_BASE_RUNTIME",environment["PYTHONPATH"])
    (package/"__init__.py").write_text("__path__.append("+repr(str(Path(base)/"biomerte"))+")\n")
    for path in paths[:-1]:(package/Path(path).name).write_bytes(payload[path])
    script=release/"rover-motion-observer.py";script.write_bytes(payload[paths[-1]])
    subprocess.run([launch["ProgramArguments"][0],"-c","from biomerte.motion_tracking import MotionRecorder; from biomerte.motion_training import TrainingStore"],
                   env=dict(os.environ,PYTHONPATH=str(release)),check=True)
    domain="gui/"+str(os.getuid())
    subprocess.run(["launchctl","bootout",domain+"/"+label],check=True)
    try:
        environment.update(PYTHONPATH=str(release),BIOME_OBSERVER_BASE_RUNTIME=base)
        launch["ProgramArguments"]=[launch["ProgramArguments"][0],str(script)]
        launch["WorkingDirectory"]=str(release)
        plist.write_bytes(plistlib.dumps(launch))
        helper.bootstrap(domain,plist)
    except Exception:
        plist.write_bytes(original);helper.bootstrap(domain,plist);raise
    print("Passive observer upgraded:",identity)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--source-root",required=True)
    upgrade(parser.parse_args().source_root)
