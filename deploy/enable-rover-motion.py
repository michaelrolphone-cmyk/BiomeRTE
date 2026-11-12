"""Enable passive motion estimation beside the current paired Rover service.

Copies the running package and adapter before overlaying the new modules; it
does not substitute an older repository runtime for deployed stream repairs.
Run with --apply after tests. No motion commands are emitted by this installer.
"""
import argparse
import hashlib
import os
import pathlib
import plistlib
import shutil
import subprocess
import tempfile
import time

def prepare(source):
    home=pathlib.Path.home()
    plist=home/"Library/LaunchAgents/org.biomerte.rover-server.plist"
    original=plist.read_bytes()
    config=plistlib.loads(original)
    current=pathlib.Path(config["ProgramArguments"][1])
    package=None
    for p in config["EnvironmentVariables"]["PYTHONPATH"].split(":"):
        if (pathlib.Path(p)/"biomerte/rover.py").is_file():
            package=pathlib.Path(p)/"biomerte"
            break
    if package is None:raise RuntimeError("Cannot identify active Rover core")
    digest=hashlib.sha256((source/"biomerte/motion_model.py").read_bytes()+
                           (source/"biomerte/motion_tracking.py").read_bytes()).hexdigest()[:12]
    release=home/".local/share/biome-rover-server/releases"/("adaptive-motion-"+digest)
    if release.exists():
        previous=release/"previous-launch-agent.plist"
        staged=release/"launch-agent.plist"
        if previous.is_file() and staged.is_file() and previous.read_bytes()==original:
            return release,plist,staged,original
        raise RuntimeError("Release exists and active service identity changed; reconcile before enabling")
    shutil.copytree(current.parent,release/"mac")
    shutil.copytree(package,release/"core/biomerte",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    for name in ("motion_model.py","motion_tracking.py"):
        shutil.copyfile(source/"biomerte"/name,release/"core/biomerte"/name)
    service=release/"mac"/current.name
    text=service.read_text()
    anchor="    application=bridge.app()"
    if text.count(anchor)!=1:raise RuntimeError("Expected exactly one active app construction")
    text=text.replace(anchor,anchor+"\n    from biomerte.motion_tracking import install_motion\n    install_motion(rover,application,home)")
    service.write_text(text)
    config["ProgramArguments"][1]=str(service)
    config["WorkingDirectory"]=str(service.parent)
    config["EnvironmentVariables"]["PYTHONPATH"]=str(release/"mac")+":"+str(release/"core")+":"+config["EnvironmentVariables"]["PYTHONPATH"]
    staged=release/"launch-agent.plist"
    staged.write_bytes(plistlib.dumps(config))
    (release/"previous-launch-agent.plist").write_bytes(original)
    return release,plist,staged,original

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    source=pathlib.Path(__file__).resolve().parents[1]
    release,plist,staged,original=prepare(source)
    print("STAGED",release,flush=True)
    subprocess.run([str(pathlib.Path.home()/".local/share/biome-robot-vision/env/bin/python"),
        "-m","compileall","-q",str(release/"core/biomerte/motion_model.py"),
        str(release/"core/biomerte/motion_tracking.py"),str(release/"mac")],check=True)
    if args.apply:
        if plist.read_bytes()!=original:raise RuntimeError("Service changed while staging; reconcile before deployment")
        temporary=plist.with_suffix(".motion.tmp")
        temporary.write_bytes(staged.read_bytes());os.replace(temporary,plist)
        result=subprocess.run(["launchctl","kickstart","-k","gui/"+str(os.getuid())+"/org.biomerte.rover-server"],capture_output=True,text=True)
        if result.returncode:
            plist.write_bytes(original)
            subprocess.run(["launchctl","kickstart","-k","gui/"+str(os.getuid())+"/org.biomerte.rover-server"],check=False)
            raise RuntimeError("Service restart rejected: "+result.stderr)
        print("APPLIED",release,flush=True)

if __name__=="__main__":main()
