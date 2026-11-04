"""Local ComfyUI orchestration; accepts image parameters, never arbitrary executable graphs."""
import base64
import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("Local redirect refused")

class ComfyBackend:
    def __init__(self, url="http://127.0.0.1:8188", model="fluxedUpFluxNSFW_71Q8GGUF.gguf"):
        if url != "http://127.0.0.1:8188":
            raise ValueError("ComfyUI must stay on loopback")
        self.url, self.model = url, model
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def api(self, path, data=None, method=None):
        payload = None if data is None else json.dumps(data).encode()
        req = Request(self.url + path, payload, {"Content-Type": "application/json"}, method=method)
        with self.http.open(req) as response:
            return json.load(response)

    def specification(self, messages):
        text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if text.lstrip().startswith("{"):
            spec = json.loads(text)
        else:
            spec = {"prompt": text}
        allowed = {"prompt", "negative_prompt", "width", "height", "steps", "seed", "batch_size", "cfg", "guidance"}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ValueError("Invalid image parameters")
        if not isinstance(spec.get("prompt"), str) or not spec["prompt"].strip():
            raise ValueError("Prompt required")
        for name, default in (("width",512),("height",512),("steps",28),("batch_size",1)):
            value = spec.setdefault(name, default)
            if type(value) is not int or value < 1 or (name in ("width","height") and value % 8):
                raise ValueError("Invalid image dimensions or sampling parameters")
        spec.setdefault("seed", secrets.randbits(63))
        if type(spec["seed"]) is not int or not 0 <= spec["seed"] < 2**64:
            raise ValueError("Invalid seed")
        spec.setdefault("negative_prompt", "")
        if not isinstance(spec["negative_prompt"], str):
            raise ValueError("Invalid negative prompt")
        spec.setdefault("cfg", 1.0)
        spec.setdefault("guidance", 3.5)
        if type(spec["guidance"]) not in (int,float) or not 0 <= spec["guidance"] <= 100:
            raise ValueError("Invalid FLUX guidance")
        if type(spec["cfg"]) not in (int,float) or not 0 <= spec["cfg"] <= 100:
            raise ValueError("Invalid guidance")
        return spec

    def graph(self, spec, token):
        return {
            "1":{"class_type":"UnetLoaderGGUF","inputs":{"unet_name":self.model}},
            "2":{"class_type":"DualCLIPLoaderGGUF","inputs":{"clip_name1":"clip_l.safetensors","clip_name2":"t5-v1_1-xxl-encoder-Q8_0.gguf","type":"flux"}},
            "3":{"class_type":"CLIPTextEncode","inputs":{"text":spec["prompt"],"clip":["2",0]}},
            "4":{"class_type":"FluxGuidance","inputs":{"conditioning":["3",0],"guidance":spec["guidance"]}},
            "5":{"class_type":"EmptySD3LatentImage","inputs":{"width":spec["width"],"height":spec["height"],"batch_size":spec["batch_size"]}},
            "6":{"class_type":"CLIPTextEncode","inputs":{"text":spec["negative_prompt"],"clip":["2",0]}},
            "7":{"class_type":"KSampler","inputs":{"model":["1",0],"positive":["4",0],"negative":["6",0],"latent_image":["5",0],"seed":spec["seed"],"steps":spec["steps"],"cfg":spec["cfg"],"sampler_name":"dpmpp_2m","scheduler":"beta","denoise":1.0}},
            "8":{"class_type":"VAELoader","inputs":{"vae_name":"ae.safetensors"}},
            "9":{"class_type":"VAEDecode","inputs":{"samples":["7",0],"vae":["8",0]}},
            "10":{"class_type":"BiomeMemoryImage","inputs":{"images":["9",0],"token":token}},
        }

    def generate(self, messages):
        spec = self.specification(messages)
        token = str(uuid.uuid4())
        prompt_id = None
        try:
            queued = self.api("/prompt", {"prompt":self.graph(spec,token),"client_id":str(uuid.uuid4())})
            prompt_id = queued["prompt_id"]
            while True:
                history = self.api("/history/" + prompt_id)
                if prompt_id in history:
                    status = history[prompt_id]["status"]
                    if status.get("status_str") == "error":
                        raise RuntimeError("Image generation failed")
                    if status.get("completed"):
                        break
                time.sleep(1)
            images = self.api("/biome/private-image/" + token)["images"]
            if not images:
                raise RuntimeError("No image returned")
            result = []
            for image in images:
                data = base64.b64decode(image, validate=True)
                if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("Invalid PNG output")
                result.append({"mime":"image/png","data":image,"sha256":hashlib.sha256(data).hexdigest()})
            return {"images":result,"seed":spec["seed"],"width":spec["width"],"height":spec["height"],"model":self.model}
        finally:
            if prompt_id:
                self.api("/history", {"delete":[prompt_id]})
            self.api("/biome/private-image/" + token, method="DELETE")
            # Drop model/latent caches between requests, releasing shared Mac GPU memory.
            self.api("/free", {"unload_models":True,"free_memory":True})
