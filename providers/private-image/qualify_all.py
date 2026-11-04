#!/usr/bin/env python3
"""Finish installation and qualify both live Biome contracts after model download."""
import base64
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import gguf
from PIL import Image,ImageStat
from private_image import gateway
from qualify import main as qualify_encrypted

ROOT=Path.home()/"Developer/biome-comfyui"
MODELS=ROOT/"ComfyUI/models"
RESOURCE=json.loads((Path.home()/".local/share/biome-private-image/manifest.json").read_text())["resource"]
EXPECTED={
 "unet/fluxedUpFluxNSFW_71Q8GGUF.gguf":"86567861427f313fbcc84f4135bc668ca2a43f01db6a8f8744f11ebdfe96ada2",
 "text_encoders/t5-v1_1-xxl-encoder-Q8_0.gguf":"9ec60f6028534b7fe5af439fcb535d75a68592a9ca3fcdeb175ef89e3ee99825",
 "text_encoders/clip_l.safetensors":"660c6f5b1abae9dc498ac2d21e1347d2abdb0cf6c0c0c8576cd796491d9a6cdd",
 "vae/ae.safetensors":"afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38"}
def call(op,args):
 return gateway.invoke(op,args)
def acquire(contract,task):
 lease=call("cap.acquire",{"contract":contract,"resource":RESOURCE,"task":task})
 while lease["state"]!="bound":
  time.sleep(2);lease=call("cap.lease",{"lease":lease["id"]})
 return lease["id"]
def wait(operation):
 while True:
  result=call("cap.status",{"operation":operation})
  if result["state"]=="succeeded":return result["result"]
  if result["state"] in ("failed","uncertain"):raise RuntimeError("Generation action failed")
  time.sleep(2)
def decode(manifest):
 data=b"".join(base64.b64decode(call("artifact.get",{"sha256":c["sha256"]})["data"],validate=True) for c in manifest["chunks"])
 assert hashlib.sha256(data).hexdigest()==manifest["sha256"]
 return data
def main():
 while not all((MODELS/name).is_file() for name in EXPECTED):
  state=call("cap.status",{"operation":"comfy-flux-q8-download-1"})
  if state["state"] in ("failed","uncertain") or (state["state"]=="succeeded" and state["result"]["exit_code"]):
   raise RuntimeError("Fluxed Up download did not finish successfully")
  time.sleep(5)
 for name,want in EXPECTED.items():
  actual=hashlib.file_digest((MODELS/name).open("rb"),"sha256").hexdigest()
  assert actual==want,"Model hash mismatch: "+name
 reader=gguf.GGUFReader(str(MODELS/"unet/fluxedUpFluxNSFW_71Q8GGUF.gguf"))
 types=Counter(t.tensor_type.name for t in reader.tensors)
 assert types["Q8_0"]>0 and set(types)<= {"Q8_0","F16","F32"},dict(types)
 print(json.dumps({"model_hashes":"verified","fluxed_up_tensor_types":dict(types)}),flush=True)
 del reader
 # Wait for readiness advertisement from the running memory-only image provider.
 while True:
  d=call("cap.discover",{"contract":"image.generate:1"})
  if any(o["resource"]==RESOURCE and o["state"]=="ready" for o in d["offers"]):break
  time.sleep(2)
 task="generic-image-qualification-"+str(uuid.uuid4())
 lease=acquire("image.generate:1",task)
 queued=None
 try:
  # Both contracts must serialize on one shared physical resource.
  queued=call("cap.acquire",{"contract":"image.private.exchange:1","resource":RESOURCE,"task":task+"-capacity"})
  assert queued["state"]=="queued","Shared GPU capacity was not enforced"
  call("cap.release",{"lease":queued["id"]});queued=None
  operation="image-generic-qualification-"+str(uuid.uuid4())
  call("cap.invoke",{"lease":lease,"operation":operation,"inputs":{"prompt":"A blue ceramic teapot on a wooden table, still life photography, no people","width":256,"height":256,"steps":4,"seed":12345}})
  result=wait(operation)
  png=decode(result["images"][0]["artifact"])
  im=Image.open(io.BytesIO(png));im.load()
  assert im.size==(256,256)
  assert sum(ImageStat.Stat(im.convert("RGB")).stddev)>2
  print(json.dumps({"generic_biome_generation":"passed","shared_gpu_capacity":"passed","png_bytes":len(png),"image_sha256":hashlib.sha256(png).hexdigest()}),flush=True)
 finally:
  if queued:call("cap.release",{"lease":queued["id"]})
  call("cap.release",{"lease":lease})
 # Hold the production admission slot while the isolated synthetic phone tests encryption.
 lease=acquire("image.generate:1","encrypted-qualification-guard-"+str(uuid.uuid4()))
 try:qualify_encrypted()
 finally:call("cap.release",{"lease":lease})
 print("BOTH_BIOME_IMAGE_CAPABILITIES_QUALIFIED",flush=True)
if __name__=="__main__":main()
