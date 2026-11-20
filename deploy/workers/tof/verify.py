#!/usr/bin/env python3
"""Observe active ToF container, native exclusion, clock and shared state."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
from urllib.request import urlopen


def native_pids(program):
    rows=subprocess.check_output(["/bin/ps","-axo","pid=,command="],text=True).splitlines()
    found=[]
    for row in rows:
        fields=row.strip().split(None,1)
        if len(fields)!=2:continue
        try:argv=shlex.split(fields[1])
        except ValueError:continue
        if program in argv and int(fields[0])!=os.getpid():found.append(int(fields[0]))
    return found


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("deployment",type=Path)
    p.add_argument("--docker",default="/usr/local/bin/docker")
    args=p.parse_args();plan=json.loads((args.deployment/"plan.json").read_text())
    if native_pids(plan["native"]["program"]):
        raise RuntimeError("Native ToF owner is still running")
    image=json.loads(subprocess.check_output(
        [args.docker,"inspect","biome-tof-coordinator"],text=True))[0]
    if image["Image"]!=plan["image_id"] or not image["State"]["Running"]:
        raise RuntimeError("Qualified ToF image is not the running owner")
    mounts={m["Destination"]:m for m in image["Mounts"]}
    for role,target in (("tof","/state/tof"),("vision","/state/vision")):
        if Path(mounts[target]["Source"]).resolve()!=Path(plan["state_paths"][role]).resolve():
            raise RuntimeError("Container and native consumers do not share the declared state")
    with urlopen(plan["health_url"]+"/readyz",timeout=3) as response:
        health=json.load(response)
    if health.get("phase")!="active" or not health.get("ready"):
        raise RuntimeError("Actual solver is not ready")
    latest_path=Path(plan["state_paths"]["tof"])/"latest.json"
    latest=json.loads(latest_path.read_text())
    age=time.time()-latest["updated_at"]
    if not 0<=age<5:
        raise RuntimeError("Native-reader solution is stale")
    clock=latest.get("transport_clock",{})
    bridge=json.loads(Path(plan["bridge"]["config"]).read_text())
    if clock.get("host_id")!=bridge["host_id"] or clock.get("raw_range_epoch")!="native_host_monotonic":
        raise RuntimeError("Raw range clock/host identity differs from native reader requirements")
    print(json.dumps(dict(schema="biomerte.tof-active-verification/1",
        checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        image_id=image["Image"],container_id=image["Id"],native_pids=[],
        health=health,solution_age_s=age,anchor_count=latest.get("anchor_count"),
        camera_id=plan["camera_id"],transport_clock=clock,
        native_reader_path=str(latest_path),same_host_state=True)))


if __name__=="__main__":main()
