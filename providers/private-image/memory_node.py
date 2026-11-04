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


# Acknowledged release runs in ComfyUI's own prompt worker: its reset clears
# execution caches as well as GPU weights. HTTP /free alone only sets a flag.
import asyncio
import gc
import execution
import comfy.model_management as model_memory

_MEMORY_RESET = 0
_original_reset = execution.PromptExecutor.reset

def _acknowledged_reset(self, *args, **kwargs):
    global _MEMORY_RESET
    result = _original_reset(self, *args, **kwargs)
    gc.collect()
    model_memory.soft_empty_cache()
    _MEMORY_RESET += 1
    return result

execution.PromptExecutor.reset = _acknowledged_reset

def _memory_state():
    running, pending = PromptServer.instance.prompt_queue.get_current_queue()
    return {'running':len(running), 'pending':len(pending),
            'loaded_models':len(model_memory.current_loaded_models),
            'reset_generation':_MEMORY_RESET}

@PromptServer.instance.routes.get('/biome/model-memory')
async def model_memory_status(request):
    if request.remote not in ('127.0.0.1','::1'): raise web.HTTPForbidden()
    return web.json_response(_memory_state())

@PromptServer.instance.routes.post('/biome/model-memory')
async def release_model_memory(request):
    if request.remote not in ('127.0.0.1','::1'): raise web.HTTPForbidden()
    body=await request.json()
    if body != {'unload_models':True}: raise web.HTTPBadRequest()
    state=_memory_state()
    if state['running'] or state['pending']: raise web.HTTPConflict()
    before=_MEMORY_RESET
    q=PromptServer.instance.prompt_queue
    q.set_flag('unload_models',True)
    q.set_flag('free_memory',True)
    while _MEMORY_RESET<=before or model_memory.current_loaded_models:
        await asyncio.sleep(.1)
    return web.json_response(_memory_state())
