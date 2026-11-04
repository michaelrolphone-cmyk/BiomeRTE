#!/usr/bin/env python3
"""Encrypted ComfyUI provider; shared phone authentication, ephemeral session keys."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import resource
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "private-chat"))
from private_chat import PrivateWorker, Remote, gateway, identity_key
from image_protocol import SessionServer, raw, b64
from comfy_backend import ComfyBackend

HOME = Path.home()
STATE = HOME / ".local/share/biome-private-image"
CHAT = HOME / ".local/share/biome-private-chat"
ROOT = HOME / "Developer/biome-comfyui"
CONTRACT = "image.private.exchange:1"
LABEL = "org.biomerte.private-image"

def image_resource(chat):
    return chat["resource"].replace("privatechat.mac.", "image.mac.", 1)

def store_bytes(encoded, remote):
    chunks = []
    for start in range(0,len(encoded),48*1024):
        chunk = encoded[start:start+48*1024]
        sha = hashlib.sha256(chunk).hexdigest()
        saved = remote.call("artifact.put", {"data":base64.b64encode(chunk).decode()})
        if saved.get("sha256") != sha:
            raise RuntimeError("Chunk verification failed")
        chunks.append({"sha256":sha,"size":len(chunk)})
    return {"sha256":hashlib.sha256(encoded).hexdigest(),"size":len(encoded),"chunks":chunks}

def store_envelope(envelope, remote):
    encoded = json.dumps(envelope, separators=(",",":")).encode()
    if len(encoded) <= 32768:
        return {"envelope":envelope}
    return {"envelope":dict(v=2,kind="artifact",sid=envelope["sid"],id=envelope["id"],
                            encoding="json",**store_bytes(encoded,remote))}

class ImageWorker(PrivateWorker):
    def advertise(self):
        for offer in self.offers:
            self.remote.call("cap.offer",dict(offer,provider=self.remote.node,state="ready"))

def serve():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    cfg = json.loads((STATE/"manifest.json").read_text())
    chat = json.loads((CHAT/"manifest.json").read_text())
    remote = Remote(cfg)
    backend = ComfyBackend()
    def enrolled(phone):
        with open(CHAT/"enrollment.lock","a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = json.loads((CHAT/"manifest.json").read_text())
            if current.get("phone_identity") not in (None,b64(phone)):
                raise ValueError("Another phone has paired")
            current["phone_identity"] = b64(phone)
            current.pop("enrollment_hash",None)
            gateway.save_json(CHAT/"manifest.json",current)
            (CHAT/"pairing.json").unlink(missing_ok=True)
    protocol = SessionServer(identity_key(),
        raw(chat["enrollment_hash"],32) if chat.get("enrollment_hash") else None,
        raw(chat["phone_identity"],32) if chat.get("phone_identity") else None,enrolled)
    def handle(action):
        if action["contract"] == "image.generate:1":
            result = backend.generate([{"role":"user","content":json.dumps(action["inputs"])}])
            for image in result["images"]:
                png = base64.b64decode(image.pop("data"),validate=True)
                image["artifact"] = store_bytes(png,remote)
            return result
        # Follow the authoritative paired phone; never reset enrollment or copy private keys.
        current = json.loads((CHAT/"manifest.json").read_text())
        protocol.phone_identity = raw(current["phone_identity"],32) if current.get("phone_identity") else None
        protocol.enrollment_hash = raw(current["enrollment_hash"],32) if current.get("enrollment_hash") else None
        envelope = protocol.handle(action["inputs"]["envelope"],action["id"],backend.generate)
        return store_envelope(envelope,remote)
    with open(STATE/"worker.lock","a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        offer = dict(contract=CONTRACT,resource=cfg["resource"],capacity=1,service="private-image",
                     configuration={"engine":"ComfyUI","model":backend.model,"encrypted":True,
                                    "protocol":2,"result_transport":"inline_or_encrypted_artifact_chunks",
                                    "reservation_policy":cfg.get("reservation_policy", {"idle_seconds":120,"release_after_result":True})})
        worker = ImageWorker(str(STATE/"worker.sqlite"),remote,[offer,dict(offer,contract="image.generate:1",service="image-generation",configuration={"engine":"ComfyUI","model":backend.model,"encrypted":False,"result_transport":"png_artifact_chunks"})],None,handler=handle)
        while True:
            try:
                nodes = backend.api("/object_info")
                if "BiomeMemoryImage" not in nodes:
                    raise RuntimeError("Memory output node unavailable")
                models = nodes["UnetLoaderGGUF"]["input"]["required"]["unet_name"][0]
                if backend.model not in models:
                    raise RuntimeError("Model unavailable")
                worker.advertise()
                break
            except Exception:
                time.sleep(2)
        while True:
            try:
                worker.once()
            except Exception:
                pass
            time.sleep(0 if getattr(gateway, 'transport_active', lambda: False)() else 2)

def install():
    os.umask(0o077)
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    chat = json.loads((CHAT/"manifest.json").read_text())
    provider = hashlib.sha256(("private-image/"+chat["provider"]).encode()).hexdigest()
    cfg = {"provider":provider,"site":chat["site"],"resource":image_resource(chat),"v":1}
    gateway.save_json(STATE/"manifest.json",cfg)
    gateway.invoke("cap.define",dict(name="image.private.exchange",version=1,
        description="ComfyUI image generation with authenticated temporary X25519 keys and AES-256-GCM image results.",
        inputs={"envelope":"obj"},outputs={"envelope":"obj"},
        usage={"before":"Use existing Private Chat v2 phone/Mac identity pairing. Fresh signed hello per generation. No plaintext request or image crosses Biome.",
               "parameters":{"envelope":"Private Chat v2 signed hello, then encrypted messages. Last user content is prompt text or image parameter JSON (prompt,width,height,steps,seed,batch_size,cfg,guidance,negative_prompt)."},
               "read_result":"Decrypt inline envelope, or retrieve all artifact chunks, verify chunk and full SHA-256, decode envelope JSON and decrypt. Response has images:[{mime,data(base64 PNG),sha256}], seed, dimensions and model. Default Fluxed Up 7.1 Q8_0, 512x512, 28 steps, DPM++ 2M / beta, FLUX guidance 3.5."}))
    gateway.invoke("cap.define",dict(name="image.generate",version=1,
        description="Generate images locally using ComfyUI and Fluxed Up GGUF Q8_0.",
        inputs={"prompt":"str","negative_prompt":"str?",
                "width":"int?","height":"int?",
                "steps":"int?","seed":"int?",
                "batch_size":"int?","cfg":"num?",
                "guidance":"num?"},
        outputs={"images":"list","seed":"int","width":"int","height":"int","model":"str"},
        usage={"example_inputs":{"prompt":"A mountain lake at sunrise","width":512,"height":512},
               "before":"Generic image generation. This contract accepts normal unencrypted parameters; Android uses the separate image.private.exchange:1 contract. Both share the same GPU resource and capacity.",
               "read_result":"images entries have mime,sha256,artifact:{sha256,size,chunks}. Retrieve each artifact.get chunk in order, base64-decode and verify SHA-256 to obtain PNG bytes."}))
    node = ROOT/"ComfyUI/custom_nodes/biome_private_image"
    node.mkdir(parents=True,exist_ok=True)
    import shutil
    shutil.copyfile(HERE/"memory_node.py",node/"__init__.py")
    common = dict(RunAtLoad=True,KeepAlive=True,
                  StandardOutPath="/dev/null",StandardErrorPath="/dev/null",
                  EnvironmentVariables={"HOME":str(HOME),"PATH":os.environ["PATH"],
                                        "PYTORCH_ENABLE_MPS_FALLBACK":"1"})
    comfy = dict(common,Label="org.biomerte.comfyui",WorkingDirectory=str(ROOT/"ComfyUI"),
                 ProgramArguments=[str(ROOT/"venv/bin/python"),str(ROOT/"ComfyUI/main.py"),
                    "--listen","127.0.0.1","--port","8188","--disable-auto-launch",
                    "--offline","--disable-metadata","--cache-none","--preview-method","none",
                    "--database-url","sqlite:///:memory:","--fp32-vae",
                    "--disable-all-custom-nodes","--whitelist-custom-nodes","biome_private_image","ComfyUI-GGUF"])
    worker = dict(common,Label=LABEL,WorkingDirectory=str(STATE),
                  ProgramArguments=[sys.executable,str(HERE/"private_image.py"),"serve"])
    agents = HOME/"Library/LaunchAgents"
    agents.mkdir(parents=True,exist_ok=True)
    for spec in (comfy,worker):
        (agents/(spec["Label"]+".plist")).write_bytes(plistlib.dumps(spec))
    print(json.dumps({"contract":CONTRACT,"resource":cfg["resource"],"pairing":"existing Private Chat identity"}))

if __name__ == "__main__":
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    {"install":install,"serve":serve}[sys.argv[1]]()

