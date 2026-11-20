#!/usr/bin/env python3
"""Apply/restore the reviewed ToF bridge settings, with compare-and-swap."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from verify import native_pids


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def replace_if_hash(target, replacement, expected):
    target, replacement = Path(target), Path(replacement)
    if sha(target.read_bytes()) != expected:
        raise RuntimeError("Bridge configuration changed since preparation; preserve it and rebase the proposal")
    raw=replacement.read_bytes()
    json.loads(raw)
    fd, temporary=tempfile.mkstemp(prefix=target.name+".tof-",dir=target.parent)
    try:
        with os.fdopen(fd,"wb") as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return sha(raw)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation",choices=("apply","rollback"))
    p.add_argument("deployment",type=Path)
    p.add_argument("--docker",default="/usr/local/bin/docker")
    args=p.parse_args()
    plan=json.loads((args.deployment/"plan.json").read_text())
    if native_pids(plan["native"]["program"]):
        raise RuntimeError("Native ToF solver must be stopped before changing UDP ownership")
    if args.operation=="apply":
        domain="gui/"+str(plan["uid"])
        disabled=subprocess.check_output(["/bin/launchctl","print-disabled",domain],text=True)
        pattern=r'"'+re.escape(plan["native"]["label"])+r'"\s*=>\s*true'
        if not re.search(pattern,disabled):
            raise RuntimeError("Native ToF launch agent must be persistently disabled before activation")
        loaded=subprocess.run(["/bin/launchctl","print",domain+"/"+plan["native"]["label"]],
                              capture_output=True,text=True)
        if loaded.returncode==0:
            raise RuntimeError("Native ToF launch agent must be booted out before activation")
        replacement=args.deployment/"config/bridge.active.json"
        expected=plan["bridge"]["before_sha256"]
    else:
        inspected=subprocess.run([args.docker,"inspect","biome-tof-coordinator"],
                                 capture_output=True,text=True)
        if inspected.returncode==0 and json.loads(inspected.stdout)[0]["State"]["Running"]:
            raise RuntimeError("Stop the container before restoring native UDP ownership")
        replacement=args.deployment/"rollback/bridge.json"
        expected=plan["bridge"]["proposed_sha256"]
    result=replace_if_hash(plan["bridge"]["config"],replacement,expected)
    print(json.dumps(dict(operation=args.operation,config_sha256=result,
                          restart_required=plan["bridge"]["label"],services_started=False)))


if __name__=="__main__":main()
