#!/usr/bin/env python3
"""Same-Mac ownership transfer using frozen configuration and current ledgers.

Preparation and read-only preflight precede any native transition. Never infer
Mac/container mutual exclusion from a shared lockfile across Docker Desktop.
"""
import argparse
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

COMPONENTS={
 "docker-cli":{"labels":["org.biomerte.mac-docker"],"state":"biome-mac-docker"},
 "storage":{"labels":["org.biomerte.mac-storage"],"state":"biome-mac-storage"},
 "replica":{"labels":["org.biomerte.mac-replica"],"state":"biome-mac-storage"},
 "gitlab":{"labels":["org.biomerte.gitlab-provider"],"state":"biome-gitlab"},
 "workspaces":{"labels":["org.biomerte.workspace-worker","org.biomerte.workspace-monitor"],"state":"biome-workspaces"},
}
DOCKER="/usr/local/bin/docker"
ADMINISTRATIVE={"docker-cli","workspaces"}
READY_SECONDS={"replica":720}
def run(argv,check=True,env=None):
    p=subprocess.run(argv,text=True,capture_output=True,env=env)
    if check and p.returncode:
        raise RuntimeError("Command failed: "+argv[0]+" "+argv[1]+"; "+p.stderr[-600:])
    return p
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,value):
    path=Path(path)
    fd,name=tempfile.mkstemp(prefix=path.name+".",dir=str(path.parent))
    try:
        with os.fdopen(fd,"w") as out:
            json.dump(value,out,indent=2);out.write("\n");out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
def persist(plan):save(Path(plan["directory"])/"plan.json",plan)
def transition(plan,action,phase="completed",**values):
    plan.setdefault("transitions",[]).append(dict(action=action,phase=phase,at=time.time(),**values))
    persist(plan)
def active_processes(paths):
    rows=[]
    for line in run(["/bin/ps","-ax","-o","pid=,command="]).stdout.splitlines():
        fields=line.strip().split(None,1)
        if len(fields)!=2:continue
        tokens=fields[1].split()
        if "-c" in tokens[:4]:continue
        if int(fields[0])!=os.getpid() and any(path in tokens for path in paths):rows.append(int(fields[0]))
    return rows
def unsettled(state):
    state=Path(state);result=[]
    for name in ("worker.sqlite","worker-v2.sqlite"):
        path=state/name
        if not path.exists():continue
        with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='actions'").fetchone():continue
            result.extend({"database":name,"operation":a,"state":s} for a,s in
                db.execute("SELECT id,state FROM actions WHERE state NOT IN ('succeeded','failed')"))
    for path in (state/"instances").glob("*/state.json"):
        value=json.loads(path.read_text())
        if value.get("active_operation") or value.get("state") in ("checkpointing","disposing","provisioning"):
            result.append({"workspace":value.get("id",path.parent.name),"state":value.get("state"),
                           "operation":value.get("active_operation")})
    # A stopped/interrupted replica must not silently discard a partial transfer.
    for path in state.glob(".sync-*"):
        if path.is_dir():result.append({"replica_transfer":path.name,"state":"sync_or_recovery_pending"})
    return result
def require_quiescent(plan):
    pending=unsettled(Path(plan["state"]))
    if pending:raise RuntimeError("Operation reconciliation required: "+json.dumps(pending))
def services(component):
    return ["workspaces","workspace-monitor"] if component=="workspaces" else [component]
def job(plan,entry):return "gui/"+str(plan["uid"])+"/"+entry["label"]
def launch_state(uid,label):
    domain="gui/"+str(uid)
    disabled=run(["/bin/launchctl","print-disabled",domain]).stdout
    return {"was_loaded":run(["/bin/launchctl","print",domain+"/"+label],False).returncode==0,
            "was_disabled":bool(re.search(r'(?m)^\s*"'+re.escape(label)+r'"\s*=>\s*(?:true|disabled)\s*$',disabled))}
def verify_native(plan,registration=False):
    for entry in plan["native"]:
        if digest(entry["path"])!=entry["sha256"]:
            raise RuntimeError("Native definition changed; preserve it for reviewed recovery")
        if registration and launch_state(plan["uid"],entry["label"])!={k:entry[k] for k in ("was_loaded","was_disabled")}:
            raise RuntimeError("Native launchd registration changed since preparation")
def literal(value,escape=True):
    if isinstance(value,str):return value.replace("$","$$") if escape else value.replace("$$","$")
    if isinstance(value,list):return [literal(x,escape) for x in value]
    if isinstance(value,dict):return {k:literal(v,escape) for k,v in value.items()}
    return value
def freeze_configuration(resolved,component,image):
    names=services(component)
    selected={name:resolved["services"][name] for name in names}
    result={k:v for k,v in resolved.items() if k not in ("services","name")}
    result["name"]="biomerte-workers";result["services"]=selected
    for spec in selected.values():
        spec.pop("build",None);spec.pop("profiles",None)
        spec["image"]=image;spec["pull_policy"]="never"
        if spec.get("depends_on"):raise ValueError("Unprepared service dependency")
        for mount in spec.get("volumes",[]):
            if not isinstance(mount,dict) or mount.get("type")!="bind":
                raise ValueError("Only explicit existing bind mounts are qualified")
            if not Path(mount["source"]).exists():raise ValueError("Required mount source is absent: "+mount["source"])
            mount.setdefault("bind",{})["create_host_path"]=False
    return literal(result)
def verify_bind_proof(value,uid,gid):
    if (value.get("schema")!="biomerte.native-bind-owner-proof/1" or value.get("passed") is not True
        or value.get("readback_ok") is not True or value.get("container_user")!="0:0"
        or value.get("mode")!="0o600" or value.get("native_uid")!=uid or value.get("expected_native_uid")!=uid
        or value.get("native_gid")!=gid or value.get("expected_native_gid")!=gid
        or not re.fullmatch(r"sha256:[0-9a-f]{64}",value.get("image_id",""))):
        raise ValueError("Administrative Docker user requires a successful native bind-owner fixture")
def verify_plan(plan):
    if plan.get("format")!=2:raise ValueError("Prepare a new format-2 handoff plan")
    for key,name in (("compose","compose.json"),("environment","environment")):
        if Path(plan[key]).resolve()!=(Path(plan["directory"])/name).resolve():raise RuntimeError("Prepared file location changed")
    for name,expected in plan["prepared_files"].items():
        if digest(Path(plan["directory"])/name)!=expected:raise RuntimeError("Prepared configuration changed: "+name)
    config=literal(json.loads(Path(plan["compose"]).read_text()),False)
    if set(config["services"])!=set(services(plan["component"])):raise RuntimeError("Prepared service set changed")
    for name,spec in config["services"].items():
        if spec["image"]!=plan["image"] or spec.get("pull_policy")!="never" or "build" in spec:
            raise RuntimeError("Prepared immutable image binding changed")
        expected="0:0" if plan["component"] in ADMINISTRATIVE else str(plan["uid"])+":"+str(plan["gid"])
        if spec.get("user")!=expected:raise RuntimeError("Prepared execution UID changed")
        for mount in spec.get("volumes",[]):
            if mount.get("type")!="bind" or mount.get("bind",{}).get("create_host_path") is not False:
                raise RuntimeError("Prepared mount admission changed")
            if not Path(mount["source"]).exists():raise RuntimeError("Required bind source disappeared")
    if plan["component"] in ADMINISTRATIVE:
        verify_bind_proof(json.loads((Path(plan["directory"])/"native-bind-owner-proof.json").read_text()),plan["uid"],plan["gid"])
def compose(plan,*argv):
    verify_plan(plan)
    return run([DOCKER,"compose","--env-file",plan["environment"],"-p","biomerte-workers","-f",plan["compose"],*argv])
def snapshot(state,target):
    target.mkdir(mode=0o700,parents=True,exist_ok=True)
    for path in state.glob("worker*.sqlite"):
        with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as source,sqlite3.connect(target/path.name) as dest:source.backup(dest)
def controller_preflight(plan):
    root=Path(__file__).resolve().parents[3];sys.path.insert(0,str(root))
    from biomerte.host_io_client import HostIOClient
    cfg=json.loads((Path(plan["client_dir"])/"host-io.json").read_text())
    u=urlsplit(cfg["url"])
    cfg["url"]=u._replace(netloc="localhost:"+str(u.port)).geturl()
    for key in ("token_file","ca_file"):
        if cfg.get(key):cfg[key]=str(Path(plan["client_dir"])/Path(cfg[key]).name)
    cfg["gateway_timeout_s"]=25
    client=HostIOClient(cfg);client.call("health",{})
    if plan["component"]!="replica":
        client.call("gateway.invoke",{"operation":"controller.invoke",
           "arguments":{"controller":cfg["host_id"],"operation":"system.info","arguments":{}},
           "request_id":"worker-handoff-preflight-"+uuid.uuid4().hex,"realm":"site"})
def inspect_containers(plan,require_recorded=False):
    ids=compose(plan,"ps","-a","-q",*services(plan["component"])).stdout.split()
    if require_recorded and set(ids)!=set(plan.get("containers",[])):
        raise RuntimeError("Container ownership set changed; do not replace or stop another owner")
    objects=[]
    for ident in ids:
        obj=json.loads(run([DOCKER,"inspect",ident]).stdout)[0]
        labels=obj.get("Config",{}).get("Labels",{})
        if (obj["Image"]!=plan["image"] or labels.get("com.docker.compose.project")!="biomerte-workers"
            or labels.get("com.docker.compose.service") not in services(plan["component"])):
            raise RuntimeError("Container image or service identity mismatch")
        expected="0:0" if plan["component"] in ADMINISTRATIVE else str(plan["uid"])+":"+str(plan["gid"])
        if obj["Config"].get("User")!=expected:raise RuntimeError("Container execution user mismatch")
        objects.append(obj)
    return objects
def objects_complete(plan,objects):
    return (len(objects)==len(services(plan["component"])) and
        {o["Config"]["Labels"]["com.docker.compose.service"] for o in objects}==set(services(plan["component"])))
def fresh_health(plan,objects):
    if not objects_complete(plan,objects):return False
    for obj in objects:
        state=obj["State"];name=obj["Config"]["Labels"]["com.docker.compose.service"]
        if not state["Running"]:return False
        if name=="workspace-monitor":continue
        if state.get("Health",{}).get("Status")!="healthy":return False
        # The marker is private container tmpfs, never the inherited native health file.
        p=run([DOCKER,"exec",obj["Id"],"python","-c",
            "import json,os; print(json.dumps(json.load(open(os.environ.get('BIOME_WORKER_HEALTH','/run/biome-health/poll.json')))))"],False)
        try:
            marker=json.loads(p.stdout)
            started=datetime.datetime.fromisoformat(state["StartedAt"].replace("Z","+00:00")).timestamp()
            if p.returncode or marker["state"]!="online" or marker["last_poll"]<started:return False
        except (ValueError,KeyError,TypeError):return False
    return True
def ready_seconds(component):return READY_SECONDS.get(component,180)
def prepare(args):
    replica_host=getattr(args,"replica_host",None)
    if args.component=="replica":
        if not replica_host:raise ValueError("Replica preparation requires a verified current --replica-host IP address")
        replica_host=str(ipaddress.ip_address(replica_host))
    elif replica_host:raise ValueError("--replica-host applies only to replica")
    home=Path(args.home).resolve();out=Path(args.output).resolve()
    out.mkdir(mode=0o700,parents=True,exist_ok=True)
    if (out/"plan.json").exists():raise ValueError("Use a new output directory; preserve the prior handoff plan")
    out.chmod(0o700);uid=os.getuid();gid=os.getgid();comp=COMPONENTS[args.component]
    state=home/".local/share"/comp["state"]
    if not state.is_dir():raise ValueError("Existing native state directory is required")
    entries=[]
    for label in comp["labels"]:
        path=home/"Library/LaunchAgents"/(label+".plist");spec=plistlib.loads(path.read_bytes())
        backup=out/(label+".plist");shutil.copyfile(path,backup);backup.chmod(0o600)
        paths=[v for v in spec["ProgramArguments"][1:] if v.endswith(".py")]
        if args.component=="docker-cli":
            launcher=state/"stdlib-launcher.json"
            if launcher.exists():paths.append(json.loads(launcher.read_text())["source"])
        entries.append(dict(launch_state(uid,label),label=label,path=str(path),backup=str(backup),
                            sha256=digest(path),argv=spec["ProgramArguments"],paths=paths))
    image="biomerte-worker-"+args.component+":"+args.version
    info=json.loads(run([DOCKER,"image","inspect",image]).stdout)[0]
    if info["Os"]!="linux" or info["Architecture"]!="arm64":raise ValueError("Qualified Mac worker image must be linux/arm64")
    cfg=json.loads((home/".local/share/biome-gitlab/config.json").read_text())
    environment={"BIOME_MAC_HOME":str(home),"BIOME_UID":str(uid),"BIOME_GID":str(gid),
       "BIOME_DOCKER_UID":"0","BIOME_DOCKER_GID":"0","BIOME_WORKER_PLATFORM":"linux/arm64",
       "BIOME_WORKER_VERSION":args.version,"BIOME_PROVIDER_HOST":os.uname().nodename,
       "BIOME_GITLAB_HOST":urlsplit(cfg["url"]).hostname,
       "BIOME_GIT_WORKSPACES":cfg.get("workspace_root",str(home/".local/share/biome-gitlab/workspaces")),
       "BIOME_HOST_IO_CLIENT_DIR":str(Path(args.client_dir).resolve())}
    if replica_host:environment["BIOME_REPLICA_HOST"]=replica_host
    envpath=out/"environment"
    if any(not isinstance(v,str) or "\n" in v or "\r" in v for v in environment.values()):raise ValueError("Invalid environment value")
    envpath.write_text("".join(k+"="+json.dumps(v)+"\n" for k,v in environment.items()));envpath.chmod(0o600)
    source=Path(__file__).with_name("compose.yaml")
    resolved=json.loads(run([DOCKER,"compose","--env-file",str(envpath),"-p","biomerte-workers",
        "-f",str(source),"--profile",args.component,"config","--format","json"],env=dict(os.environ,**environment)).stdout)
    prepared=out/"compose.json";save(prepared,freeze_configuration(resolved,args.component,info["Id"]))
    files={"compose.json":digest(prepared),"environment":digest(envpath)}
    if args.component in ADMINISTRATIVE:
        proof=Path(args.docker_bind_proof) if args.docker_bind_proof else home/".local/share/biomerte-worker-containers/native-bind-owner-proof.json"
        value=json.loads(proof.read_text());verify_bind_proof(value,uid,gid)
        save(out/"native-bind-owner-proof.json",value);files["native-bind-owner-proof.json"]=digest(out/"native-bind-owner-proof.json")
    plan={"format":2,"component":args.component,"home":str(home),"uid":uid,"gid":gid,"state":str(state),
          "native":entries,"native_paths":[p for e in entries for p in e["paths"]],
          "compose":str(prepared),"environment":str(envpath),"prepared_files":files,
          "client_dir":environment["BIOME_HOST_IO_CLIENT_DIR"],"image":info["Id"],"version":args.version,
          "directory":str(out),"status":"prepared","containers":[],"transitions":[],"native_transition_started":False}
    if replica_host:
        plan["replica_source"]={"host":replica_host,"volume":json.loads((state/"replica.json").read_text())["volume"],
                                "host_key_alias":"biome-sacred-pi"}
    persist(plan);compose(plan,"config","--quiet")
    print(json.dumps({"prepared":str(out/"plan.json"),"native_pids":active_processes(plan["native_paths"]),
                      "unsettled":unsettled(state),"image":plan["image"]}))
def restore_native(plan):
    verify_native(plan)
    for entry in plan["native"]:
        current=launch_state(plan["uid"],entry["label"]);target=job(plan,entry)
        if current["was_loaded"] and not entry["was_loaded"]:
            raise RuntimeError("Unexpected loaded native job; preserve it for reviewed recovery")
        if entry["was_loaded"] and not current["was_loaded"]:
            if active_processes(entry.get("paths",[])):
                raise RuntimeError("Native child still owns state; do not bootstrap another owner")
            transition(plan,"enable_for_restore","intent",label=entry["label"])
            run(["/bin/launchctl","enable",target])
            transition(plan,"bootstrap","intent",label=entry["label"])
            run(["/bin/launchctl","bootstrap","gui/"+str(plan["uid"]),entry["path"]])
            transition(plan,"bootstrap",label=entry["label"])
        action="disable" if entry["was_disabled"] else "enable"
        transition(plan,"restore_registration","intent",label=entry["label"])
        run(["/bin/launchctl",action,target])
        if launch_state(plan["uid"],entry["label"])!={k:entry[k] for k in ("was_loaded","was_disabled")}:
            raise RuntimeError("Original launchd registration was not restored")
        transition(plan,"restore_registration",label=entry["label"])
def restore(plan):
    verify_plan(plan);verify_native(plan);require_quiescent(plan)
    objects=inspect_containers(plan,require_recorded=True)
    # Journal before the command; its return value cannot prove effect absence.
    transition(plan,"stop_containers","intent")
    running=[o["Id"] for o in objects if o["State"]["Running"]]
    if running:run([DOCKER,"stop","--time","-1",*running])
    if any(o["State"]["Running"] for o in inspect_containers(plan,require_recorded=True)):
        raise RuntimeError("Replacement container still owns state")
    transition(plan,"stop_containers")
    require_quiescent(plan)
    restore_native(plan)
    plan["status"]="native_restored";plan["native_transition_started"]=False;persist(plan)
def apply(plan):
    verify_plan(plan)
    if plan["status"] not in ("prepared","preflight_failed","native_restored"):
        raise RuntimeError("Plan already transitioned; inspect it or use explicit rollback")
    if any(e["was_disabled"] for e in plan["native"]):
        raise RuntimeError("Prepared native owner was disabled; do not activate implicitly")
    if any(not e["was_loaded"] for e in plan["native"]):
        raise RuntimeError("Prepared native owner was unloaded; do not activate implicitly")
    verify_native(plan,registration=True);require_quiescent(plan);controller_preflight(plan)
    before=inspect_containers(plan)
    if any(o["State"]["Running"] for o in before):
        raise RuntimeError("A replacement owner already exists; do not recreate it")
    compose(plan,"create","--no-build",*services(plan["component"]))
    created=inspect_containers(plan)
    if not objects_complete(plan,created) or any(o["State"]["Running"] for o in created):
        raise RuntimeError("Prepared container set is incomplete or already running")
    plan["containers"]=[o["Id"] for o in created];plan["status"]="containers_created";persist(plan)
    require_quiescent(plan)
    plan["native_transition_started"]=True;plan["status"]="native_transition";persist(plan)
    for entry in plan["native"]:
        target=job(plan,entry)
        transition(plan,"disable","intent",label=entry["label"])
        run(["/bin/launchctl","disable",target]);transition(plan,"disable",label=entry["label"])
        if launch_state(plan["uid"],entry["label"])["was_loaded"]:
            transition(plan,"bootout","intent",label=entry["label"])
            run(["/bin/launchctl","bootout",target]);transition(plan,"bootout",label=entry["label"])
    plan["status"]="native_stopped";persist(plan)
    deadline=time.monotonic()+45
    while active_processes(plan["native_paths"]) and time.monotonic()<deadline:time.sleep(.5)
    if active_processes(plan["native_paths"]):raise RuntimeError("Native owner still running; replacement not started")
    require_quiescent(plan);snapshot(Path(plan["state"]),Path(plan["directory"])/"ledger-before-container")
    inspect_containers(plan,require_recorded=True)
    transition(plan,"start_containers","intent")
    run([DOCKER,"start",*plan["containers"]])
    plan["status"]="container_started";persist(plan);transition(plan,"start_containers")
    deadline=time.monotonic()+ready_seconds(plan["component"])
    while time.monotonic()<deadline:
        objects=inspect_containers(plan,require_recorded=True)
        if fresh_health(plan,objects):
            plan["status"]="container_healthy";plan["verified_at"]=time.time();persist(plan)
            print(json.dumps({"component":plan["component"],"status":plan["status"],"containers":plan["containers"]}));return
        if any(not o["State"]["Running"] for o in objects):break
        time.sleep(3)
    raise RuntimeError("Container readiness was not established")
def execute(plan,action):
    # Re-applying an already transferred plan must not roll its healthy owner back.
    if action=="apply" and (plan.get("status") not in ("prepared","preflight_failed","native_restored") or plan.get("native_transition_started")):
        raise RuntimeError("Plan already transitioned; inspect it or use explicit rollback")
    try:
        return (apply if action=="apply" else restore)(plan)
    except Exception as exc:
        plan["last_error"]=type(exc).__name__+": "+str(exc);plan["failed_phase"]=plan.get("status")
        try:
            pending=unsettled(Path(plan["state"]))
        except Exception as scan_error:
            pending=[{"state":"custody_unreadable","error":type(scan_error).__name__}]
        plan["status"]="requires_reconciliation" if pending else (
            "handoff_failed" if plan.get("native_transition_started") else "preflight_failed")
        persist(plan)
        if action=="apply" and plan.get("native_transition_started") and not pending:
            try:restore(plan)
            except Exception as recovery:
                plan["status"]="recovery_blocked";plan["recovery_error"]=type(recovery).__name__+": "+str(recovery);persist(plan)
        raise
def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest="action",required=True)
    q=sub.add_parser("prepare");q.add_argument("component",choices=COMPONENTS);q.add_argument("--home",default=str(Path.home()))
    q.add_argument("--version",required=True);q.add_argument("--client-dir",required=True);q.add_argument("--output",required=True)
    q.add_argument("--docker-bind-proof",help="Existing successful native bind-owner fixture receipt")
    q.add_argument("--replica-host",help="Current Pi IP verified against its gateway registration; replica only")
    for mode in ("apply","rollback"):
        q=sub.add_parser(mode);q.add_argument("plan")
    args=p.parse_args()
    if args.action=="prepare":prepare(args);return
    plan=json.loads(Path(args.plan).read_text())
    lockroot=Path(plan["home"])/".local/share/biomerte-worker-containers/handoff-locks"
    lockroot.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (lockroot/(plan["component"]+".lock")).open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        execute(plan,args.action)
if __name__=="__main__":main()
