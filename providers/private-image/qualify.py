#!/usr/bin/env python3
"""Live Biome encrypted-image qualification with an isolated synthetic phone identity."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import uuid
from PIL import Image, ImageStat
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from private_image import PrivateWorker, Remote, gateway, store_envelope, ComfyBackend
from image_protocol import SessionServer, public
from image_client import SessionClient

def wait(remote,operation):
    while True:
        result=remote.call("cap.status",{"operation":operation})
        if result["state"]=="succeeded": return result["result"]
        if result["state"] in ("failed","uncertain"): raise RuntimeError("Qualification request failed")
        time.sleep(2)

def exchange(remote,resource,envelope):
    task="qualify-"+envelope["id"]
    lease=remote.call("cap.acquire",{"contract":"image.private.exchange:1","resource":resource,"task":task})
    lid=lease["id"]
    try:
        while lease["state"]!="bound":
            time.sleep(2);lease=remote.call("cap.lease",{"lease":lid})
        remote.call("cap.invoke",{"lease":lid,"operation":envelope["id"],"inputs":{"envelope":envelope}})
        return wait(remote,envelope["id"])["envelope"]
    finally:
        remote.call("cap.release",{"lease":lid})

def main():
    os.umask(0o077)
    cfg=json.loads((Path.home()/".local/share/biome-private-image/manifest.json").read_text())
    suffix=secrets.token_hex(12)
    cfg.update(provider=hashlib.sha256(("image-qualification/"+suffix).encode()).hexdigest(),
               resource="image.qualification."+suffix)
    remote=Remote(cfg)
    server_identity,phone=Ed25519PrivateKey.generate(),Ed25519PrivateKey.generate()
    server=SessionServer(server_identity,phone_identity=public(phone))
    client=SessionClient(public(server_identity),phone)
    backend=ComfyBackend()
    stop=threading.Event()
    with tempfile.TemporaryDirectory(prefix="biome-image-qualification-") as tmp:
        offer=dict(contract="image.private.exchange:1",resource=cfg["resource"],capacity=1,service="image-qualification")
        def pump():
            worker=PrivateWorker(str(Path(tmp)/"metadata.sqlite"),remote,[offer],None,
                handler=lambda a:store_envelope(server.handle(a["inputs"]["envelope"],a["id"],backend.generate),remote))
            worker.advertise()
            while not stop.is_set():
                try: worker.once()
                except Exception: pass
                stop.wait(1)
        thread=threading.Thread(target=pump,daemon=True);thread.start()
        try:
            client.accept(exchange(remote,cfg["resource"],client.hello))
            prompt="A blue ceramic teapot on a wooden table, still life photography, no people"
            request=client.request([{"role":"user","content":json.dumps({"prompt":prompt,"width":256,"height":256,"steps":4,"seed":12345})}])
            received=exchange(remote,cfg["resource"],request)
            if received.get("kind")=="artifact":
                data=b"".join(base64.b64decode(remote.call("artifact.get",{"sha256":c["sha256"]})["data"],validate=True) for c in received["chunks"])
                assert hashlib.sha256(data).hexdigest()==received["sha256"]
                received=json.loads(data)
            clear=client.response(received)
            assert "error" not in clear,clear.get("error")
            image=clear["images"][0]
            png=base64.b64decode(image["data"],validate=True)
            assert hashlib.sha256(png).hexdigest()==image["sha256"]
            im=Image.open(io.BytesIO(png));im.load()
            assert im.size==(256,256)
            assert sum(ImageStat.Stat(im.convert("RGB")).stddev)>2
            assert backend.api("/history")=={}
            assert prompt.encode() not in b"".join(p.read_bytes() for p in Path(tmp).iterdir() if p.is_file())
            print(json.dumps({"encrypted_biome_roundtrip":"passed","dimensions":list(im.size),
                              "png_bytes":len(png),"image_sha256":image["sha256"],
                              "comfy_history_cleared":True,"mac_ledger_content_absent":True}))
        finally:
            stop.set();thread.join(10)
            discovered=remote.call("cap.discover",{"contract":"image.private.exchange:1"})
            for off in discovered["offers"]:
                if off["resource"]==cfg["resource"]: remote.call("cap.delete",{"offer":off["id"]})

if __name__=="__main__":main()
