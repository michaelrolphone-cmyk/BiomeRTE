#!/usr/bin/env python3
"""Prepare the two private workers together; never stop/start services or enroll."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys

LABELS = {"chat":"org.biomerte.private-chat","image":"org.biomerte.private-image"}
DOCKER = "/usr/local/bin/docker"
REPO = Path(__file__).resolve().parents[3]

def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n")
    path.chmod(0o600)

def disabled_override(text, label):
    marker = '"'+label+'"'
    pattern = r'^\s*"'+re.escape(label)+r'"\s*=>\s*(true|false|enabled|disabled)\s*,?\s*$'
    match = re.search(pattern,text,re.M)
    if match:
        return match.group(1)
    if marker in text:
        raise ValueError("Unknown explicit LaunchAgent disabled state")
    return None

def launchd_snapshot(label, uid, plist_disabled=False):
    text = subprocess.check_output(["/bin/launchctl","print-disabled",f"gui/{uid}"],text=True)
    override = disabled_override(text,label)
    enabled = override in ("enabled","false") if override is not None else not plist_disabled
    job = subprocess.run(["/bin/launchctl","print",f"gui/{uid}/{label}"],capture_output=True,text=True)
    match = re.search(r"^\s*pid = (\d+)\s*$",job.stdout,re.M)
    return {"label":label,"enabled":enabled,"disabled_override":override,
            "loaded":job.returncode==0,"pid":int(match.group(1)) if match else None}


def identity(chat, image):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    c = json.loads((chat/"manifest.json").read_text())
    i = json.loads((image/"manifest.json").read_text())
    key = chat/"identity.ed25519"
    mode = key.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("Existing signing identity must be owner-only")
    public = Ed25519PrivateKey.from_private_bytes(key.read_bytes()).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    if c["site"] != i["site"]:
        raise ValueError("Private workers belong to different sites")
    import base64
    phone = base64.b64decode(c["phone_identity"],validate=True) if c.get("phone_identity") else None
    return {"site":c["site"],"chat_provider":c["provider"],"image_provider":i["provider"],
            "chat_resource":c["resource"],"image_resource":i["resource"],
            "signing_public_sha256":hashlib.sha256(public).hexdigest(),
            "phone_public_sha256":hashlib.sha256(phone).hexdigest() if phone else None,
            "enrollment_pending":bool(c.get("enrollment_hash"))}

def inspect_image(image):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}",image):
        raise ValueError("An immutable local image ID is required")
    result = json.loads(subprocess.check_output([DOCKER,"image","inspect",image],text=True))[0]
    if result["Id"] != image or result["Os"] != "linux" or result["Architecture"] != "arm64":
        raise ValueError("Expected the qualified linux/arm64 worker image")
    return {"id":image,"os":result["Os"],"architecture":result["Architecture"],
            "source_commit":result.get("Config",{}).get("Labels",{}).get("org.opencontainers.image.revision")}

def image_identity_preflight(image, kind, chat, state, expected):
    # Run only imports and identity reads, with both native state mounts read-only.
    # Do not serve, enroll, call the gateway, or contact a model backend.
    code = """
import base64,hashlib,importlib.util,json,os,sys
from pathlib import Path
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
sys.path.insert(0,'/opt/biome/providers/private-chat')
import private_chat
c=json.loads((Path('/state/chat')/'manifest.json').read_text())
i=json.loads((Path('/state/image')/'manifest.json').read_text())
public=private_chat.identity_key().public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
phone=base64.b64decode(c['phone_identity'],validate=True) if c.get('phone_identity') else None
spec=importlib.util.spec_from_file_location('qualified_private_health','/opt/private-health.py')
health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)
value={'site':c['site'],'chat_provider':c['provider'],'image_provider':i['provider'],
 'chat_resource':c['resource'],'image_resource':i['resource'],
 'signing_public_sha256':hashlib.sha256(public).hexdigest(),
 'phone_public_sha256':hashlib.sha256(phone).hexdigest() if phone else None,
 'enrollment_pending':bool(c.get('enrollment_hash'))}
print(json.dumps({'identity':value,'uid':os.getuid(),'health_module_readable':True,
 'worker_source':private_chat.__file__}))
"""
    command = [DOCKER,"run","--rm","--user",f"{os.getuid()}:{os.getgid()}",
               "--read-only","--network","none","--cap-drop","ALL",
               "--security-opt","no-new-privileges:true","-e","HOME=/home/biome",
               "--mount",f"type=bind,source={chat},target=/state/chat,readonly",
               "--mount",f"type=bind,source={state},target=/state/image,readonly",
               "--entrypoint","python",image,"-c",code]
    result = json.loads(subprocess.check_output(command,text=True))
    if (result["identity"] != expected or result["uid"] != os.getuid()
            or not result["worker_source"].startswith("/opt/biome/")
            or result["health_module_readable"] is not True):
        raise ValueError("Container identity or worker health import failed")
    return {"kind":kind,"image":image,"uid":result["uid"],"identity_preserved":True,
            "health_module_readable":True,"native_state_read_only":True,"network":"none"}


def prepare(args):
    os.umask(0o077)
    repo = Path(args.repo).resolve()
    out = Path(args.output).resolve()
    if out.exists():
        raise ValueError("Use a new deployment directory")
    chat = Path.home()/".local/share/biome-private-chat"
    image = Path.home()/".local/share/biome-private-image"
    before = identity(chat,image)
    client = json.loads(Path(args.client_config).read_text())
    sys.path.insert(0,str(repo))
    from biomerte.host_io_client import HostIOClient
    from biomerte.model_host_io import require_model_transport
    require_model_transport(HostIOClient(client))
    if not Path(client["ca_file"]).is_file() or not Path(client["token_file"]).is_file():
        raise ValueError("Existing TLS CA and HostIO credential are required")
    images = {"chat":inspect_image(args.chat_image),"image":inspect_image(args.image_image)}
    out.mkdir(parents=True,mode=0o700)
    (out/"config").mkdir(mode=0o700)
    (out/"private").mkdir(mode=0o700)
    (out/"rollback").mkdir(mode=0o700)
    shutil.copyfile(client["token_file"],out/"private/host-io.token")
    (out/"private/host-io.token").chmod(0o600)
    shutil.copyfile(client["ca_file"],out/"config/host-io-ca.pem")
    native = dict(client, url=args.native_url, token_file=str(out/"private/host-io.token"),
                  ca_file=str(out/"config/host-io-ca.pem"))
    write(out/"config/host-io.native.json",native)
    container = dict(client,url=args.container_url,token_file="/run/biome/host-io.token",ca_file="/run/biome/host-io-ca.pem")
    require_model_transport(HostIOClient(dict(container,token_file=native["token_file"],ca_file=native["ca_file"])))
    write(out/"config/host-io.json",container)
    source_root = Path(args.model_source_root).resolve() if args.model_source_root else repo
    relative = {"chat_module":"providers/private-chat/model_service.py",
                "image_module":"providers/private-image/comfy_backend.py",
                "runtime_module":"providers/model-runtime/model_runtime.py"}
    sources = {key:source_root/name for key,name in relative.items()}
    for key,path in sources.items():
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256((repo/relative[key]).read_bytes()).digest():
            raise ValueError("Native release model source differs from the qualified source")
    model_config = dict(enabled=True,**{key:str(path) for key,path in sources.items()},
        sha256={key:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in sources.items()})
    write(out/"config/bridge.models.patch.json",{"models":model_config})
    uid,gid = os.getuid(),os.getgid()
    def bind(source,target,readonly=False):
        return {"type":"bind","source":str(source),"target":target,"read_only":readonly,
                "bind":{"create_host_path":False}}
    common = {"platform":"linux/arm64","user":f"{uid}:{gid}","init":True,
        "read_only":True,"restart":"unless-stopped","cap_drop":["ALL"],
        "security_opt":["no-new-privileges:true"],
        "ulimits":{"core":{"soft":0,"hard":0}},
        "tmpfs":["/tmp:rw,noexec,nosuid,mode=1777",
                 f"/run/biome-health:rw,noexec,nosuid,uid={uid},gid={gid},mode=0700"],
        "environment":{"HOME":"/home/biome","BIOME_PROVIDER_HOST":socket.gethostname(),
                       "BIOME_GATEWAY_HOST_IO_CONFIG":"/run/biome/host-io.json",
                       "BIOME_MODEL_HOST_IO_CONFIG":"/run/biome/host-io.json",
                       "BIOME_PRIVATE_CHAT_STATE":"/state/chat","BIOME_PRIVATE_IMAGE_STATE":"/state/image",
                       "BIOME_PRIVATE_MODEL_HEALTH":"/run/biome-health/model.json"},
        "volumes":[bind(out/"config/host-io.json","/run/biome/host-io.json",True),
                   bind(out/"config/host-io-ca.pem","/run/biome/host-io-ca.pem",True),
                   bind(out/"private/host-io.token","/run/biome/host-io.token",True),
                   bind(chat,"/state/chat")]}
    services = {}
    native_launchd = {}
    for kind in ("chat","image"):
        service = json.loads(json.dumps(common))
        service.update(image=images[kind]["id"],command=["private-"+kind])
        if kind == "image":
            service["volumes"].append(bind(image,"/state/image"))
        services["private-"+kind] = service
        source = Path.home()/"Library/LaunchAgents"/(LABELS[kind]+".plist")
        spec = plistlib.loads(source.read_bytes())
        if spec.get("Label") != LABELS[kind]:
            raise ValueError("Native LaunchAgent identity mismatch")
        shutil.copyfile(source,out/"rollback"/("native."+kind+".plist"))
        native_launchd[kind] = launchd_snapshot(LABELS[kind],uid,bool(spec.get("Disabled",False)))
    write(out/"compose.json",{"name":"biome-private","services":services})
    revision = subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip()
    plan = {"v":1,"state":"prepared_not_migrated","repo":str(repo),"preparation_source_commit":revision,
        "native_model_source_root":str(source_root),
        "uid":uid,"gid":gid,"identity":before,"images":images,"native_labels":LABELS,
        "native_launchd":native_launchd,
        "chat_state":str(chat),"image_state":str(image),
        "native_admission":str(Path.home()/".local/share/biome-model-runtime/admission.sqlite"),
        "project":"biome-private","same_mac_only":True,
        "native_dependencies":["LM Studio","ComfyUI","native ModelRuntime","HostIO TLS listener"],
        "native_model_state_is_not_mounted":True,
        "cross_kernel_file_locks_are_not_fencing":True}
    subprocess.run([DOCKER,"compose","-f",str(out/"compose.json"),"config","--quiet"],check=True)
    preflight = [image_identity_preflight(images[kind]["id"],kind,chat,image,before)
                 for kind in ("chat","image")]
    if identity(chat,image) != before:
        raise ValueError("Private identity changed during preparation")
    write(out/"identity-preflight.json",{"checks":preflight,"model_generation_performed":False})
    write(out/"plan.json",plan)
    print(json.dumps({"deployment":str(out),"state":plan["state"],
                      "image_ids":{key:value["id"] for key,value in images.items()},
                      "identity_preserved":True}))

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo",default=str(REPO))
    p.add_argument("--output",required=True)
    p.add_argument("--client-config",required=True)
    p.add_argument("--native-url",default="https://127.0.0.1:8766")
    p.add_argument("--container-url",default="https://host.docker.internal:8766")
    p.add_argument("--model-source-root")
    p.add_argument("--chat-image",required=True)
    p.add_argument("--image-image",required=True)
    prepare(p.parse_args())
