"""ComfyUI output node: PNG bytes stay in RAM until collected by the local provider."""
import io
import uuid
import numpy as np
from PIL import Image
from aiohttp import web
from server import PromptServer

RESULTS = {}

class BiomeMemoryImage:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",), "token": ("STRING", {"default": ""})}}
    RETURN_TYPES = ()
    FUNCTION = "collect"
    OUTPUT_NODE = True
    CATEGORY = "Biome/private"

    def collect(self, images, token):
        if str(uuid.UUID(token)) != token:
            raise ValueError("Invalid result token")
        output = []
        for frame in images:
            pixels = np.clip(frame.cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
            stream = io.BytesIO()
            Image.fromarray(pixels).save(stream, format="PNG")
            output.append(stream.getvalue())
        RESULTS[token] = output
        return {"ui": {}}

@PromptServer.instance.routes.get("/biome/private-image/{token}")
async def take_result(request):
    token = request.match_info["token"]
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    output = RESULTS.pop(token, None)
    if output is None:
        raise web.HTTPNotFound()
    import base64
    return web.json_response({"images": [base64.b64encode(v).decode("ascii") for v in output]})

@PromptServer.instance.routes.delete("/biome/private-image/{token}")
async def delete_result(request):
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden()
    RESULTS.pop(request.match_info["token"], None)
    return web.json_response({"ok": True})

NODE_CLASS_MAPPINGS = {"BiomeMemoryImage": BiomeMemoryImage}
