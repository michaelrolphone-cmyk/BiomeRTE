#!/usr/bin/env python3
"""Read-only handoff checks and a stopped-owner snapshot. Never changes owners."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from prepare import identity, write, DOCKER

def database(path, query):
    if not path.exists():return []
    with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
        return db.execute(query).fetchall()

def owners(plan):
    pids = {}
    for kind,label in plan["native_labels"].items():
        result = subprocess.run(["/bin/launchctl","print",f"gui/{plan['uid']}/{label}"],
                                capture_output=True,text=True)
        match = re.search(r"^\s*pid = (\d+)\s*$",result.stdout,re.M)
        if match:pids[kind] = int(match.group(1))
    # Also catch a manually launched native worker outside its LaunchAgent.
    output = subprocess.check_output(["/bin/ps","-axo","pid=,command="],text=True)
    for line in output.splitlines():
        pid,_,command = line.strip().partition(" ")
        if re.search(r"(?:^|/)(?:private_chat|private_image)\.py\s+serve(?:\s|$)",command):
            pids["process-"+pid] = int(pid)
    ids = subprocess.check_output([DOCKER,"ps","-q"],text=True).split()
    containers = []
    if ids:
        values = json.loads(subprocess.check_output([DOCKER,"inspect",*ids],text=True))
        roots = {str(Path(plan["chat_state"]).resolve()),str(Path(plan["image_state"]).resolve())}
        for row in values:
            if any(m.get("RW") and str(Path(m["Source"]).resolve()) in roots for m in row.get("Mounts",[])):
                containers.append({"id":row["Id"],"name":row["Name"].lstrip("/"),
                    "image":row["Image"],"state":row["State"]["Status"],
                    "health":row["State"].get("Health",{}).get("Status"),
                    "service":row.get("Config",{}).get("Labels",{}).get("com.docker.compose.service"),
                    "project":row.get("Config",{}).get("Labels",{}).get("com.docker.compose.project")})
    return pids,containers

def status(out, plan):
    sys.path.insert(0,plan["repo"])
    from biomerte.host_io_client import HostIOClient
    chat,image = Path(plan["chat_state"]),Path(plan["image_state"])
    pids,containers = owners(plan)
    native_queue = database(Path(plan["native_admission"]),"SELECT pid,kind,phase FROM queue ORDER BY ticket")
    uncertain = {}
    for kind,path in (("chat",chat/"worker-v2.sqlite"),("image",image/"worker.sqlite")):
        rows = database(path,"SELECT COUNT(*) FROM actions WHERE state='uncertain'")
        uncertain[kind] = rows[0][0] if rows else 0
    client = HostIOClient.from_config(out/"config/host-io.native.json")
    model = client.call("model.status")
    return {"native_pids":pids,"containers":containers,
        "native_admission":[{"pid":r[0],"kind":r[1],"phase":r[2]} for r in native_queue],
        "uncertain_actions":uncertain,"model_bridge":model,
        "model_sources_match":model.get("source_sha256")==json.loads((out/"config/bridge.models.patch.json").read_text())["models"]["sha256"],
        "identity_matches":identity(chat,image)==plan["identity"]}

def capture(out, plan, observed):
    if (observed["native_pids"] or observed["containers"] or observed["native_admission"] or
            any(observed["uncertain_actions"].values()) or
            observed["model_bridge"]["active_requests"] or not observed["identity_matches"] or not observed["model_sources_match"]):
        raise RuntimeError("Handoff requires both native workers stopped, all inference drained, and no container writer")
    target = out/"rollback/state"
    if target.exists():raise RuntimeError("A stopped-owner snapshot already exists")
    target.mkdir(parents=True,mode=0o700)
    files = []
    for kind,root,names in (
        ("chat",Path(plan["chat_state"]),["manifest.json","identity.ed25519","privacy.json","pairing.json","worker-v2.sqlite"]),
        ("image",Path(plan["image_state"]),["manifest.json","worker.sqlite"])):
        destination = target/kind;destination.mkdir(mode=0o700)
        for name in names:
            source = root/name
            if not source.exists():continue
            saved = destination/name
            if source.suffix == ".sqlite":
                with sqlite3.connect(source.as_uri()+"?mode=ro",uri=True) as src, sqlite3.connect(saved) as dst:
                    src.backup(dst)
            else:
                shutil.copyfile(source,saved)
            saved.chmod(0o600)
            files.append(kind+"/"+name)
    receipt = {"captured_at":time.time(),"identity":plan["identity"],"files":files,
               "owners_stopped":True,"inflight_drained":True,
               "cross_file_atomic":False,"automatic_restore":False}
    write(out/"rollback/capture.json",receipt)
    return {"captured":True,"files":files,"identity_preserved":True}

def verify(out,plan,observed):
    if (observed["native_pids"] or not observed["identity_matches"] or not observed["model_sources_match"]
            or observed["model_bridge"].get("runtime_domain")!="native-mac"):
        raise RuntimeError("Native ownership or private identity mismatch")
    rows = observed["containers"]
    if len(rows) != 2:
        raise RuntimeError("Expected exactly the two private container writers")
    for kind in ("chat","image"):
        matching = [row for row in rows if row["project"]==plan["project"] and row["service"]=="private-"+kind]
        if len(matching)!=1 or matching[0]["image"]!=plan["images"][kind]["id"] or matching[0]["health"]!="healthy":
            raise RuntimeError("Private worker image, ownership or health is not verified")
    result = {"verified_at":time.time(),"state":"container_workers_live_native_models",
              "identity_preserved":True,"container_ids":[row["id"] for row in rows],
              "native_backend_migrated":False,"android_device_test_performed":False}
    write(out/"verification.json",result)
    return result

if __name__ == "__main__":
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("deployment")
    p.add_argument("action",choices=("status","capture","verify"))
    a=p.parse_args();out=Path(a.deployment).resolve()
    plan=json.loads((out/"plan.json").read_text())
    observed=status(out,plan)
    result=capture(out,plan,observed) if a.action=="capture" else verify(out,plan,observed) if a.action=="verify" else observed
    print(json.dumps(result,sort_keys=True))
