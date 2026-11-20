"""Native model custody and the existing HostIO model client.

Worker sessions, signing identity, encryption, ledgers and artifact delivery stay
in the worker. Only admitted model execution lives here on the native Mac.
No prompt, response, progress payload or session key is persisted by this module.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import uuid
import os
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

IMAGE_MODEL = "fluxedUpFluxNSFW_71Q8GGUF.gguf"
OPERATIONS = frozenset(("chat.privacy", "chat.models", "chat.infer",
                        "image.ready", "image.generate"))
PUBLIC_ERRORS = frozenset(("selected_model_required", "selected_model_unavailable",
                          "model_load_failed", "model_response_mismatch"))


class ModelHostError(RuntimeError):
    def __init__(self, code="private_inference_unavailable"):
        self.public_code = code if code in PUBLIC_ERRORS else "private_inference_unavailable"
        super().__init__(self.public_code)


def require_model_transport(client):
    # A bearer token alone does not encrypt private model traffic. This channel
    # always uses the existing HostIO TLS listener, including on Docker Desktop.
    if urlsplit(client.url).scheme != "https":
        raise ValueError("Private model traffic requires HTTPS")
    if not isinstance(client.config.get("host_id"), str) or not client.config["host_id"]:
        raise ValueError("Private model traffic requires a pinned host identity")


class ModelHostClient:
    """Actual worker-side backend adapter; never opens native admission state."""
    model = IMAGE_MODEL

    def __init__(self, client):
        require_model_transport(client)
        self.client = client

    def privacy_ready(self):
        return self.client.model_call("chat.privacy", {})

    def catalog(self):
        return self.client.model_call("chat.models", {})

    def inference(self, messages, model=None):
        return self.client.model_call("chat.infer", {"messages":messages, "model":model})

    def ready(self):
        result = self.client.model_call("image.ready", {})
        if result.get("ready") is not True or result.get("model") != self.model or result.get("output") != "memory":
            raise RuntimeError("Native image backend identity is unavailable")
        return result

    def generate(self, messages, progress=None):
        result = self.client.model_call("image.generate", {"messages":messages}, progress=progress)
        if result.get("model") != self.model:
            raise ModelHostError("model_response_mismatch")
        return result


@contextmanager
def request_health(request_id):
    """A tmpfs metadata receipt only; no model input or output is stored."""
    configured = os.environ.get("BIOME_PRIVATE_MODEL_HEALTH")
    path = Path(configured) if configured else None
    temporary = None
    if path is not None:
        import tempfile
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name+".")
        try:
            with os.fdopen(fd, "w") as target:
                json.dump({"request_id":request_id, "worker_pid":os.getpid(),
                           "started_at":time.time()}, target)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    try:
        yield
    finally:
        if path is not None:
            try:
                current = json.loads(path.read_text())
                if current.get("request_id") == request_id:
                    path.unlink()
            except (FileNotFoundError, ValueError):
                pass


def _load_source(config, key, name):
    path = Path(config.get(key, ""))
    expected = config.get("sha256", {}).get(key)
    if (not path.is_absolute() or not path.is_file() or
            not isinstance(expected, str) or len(expected) != 64 or
            hashlib.sha256(path.read_bytes()).hexdigest() != expected):
        raise ValueError("Native model source identity mismatch")
    prior = sys.modules.get(name)
    if prior is not None:
        if Path(prior.__file__).resolve() != path.resolve():
            raise ValueError("Native model module identity conflict")
        return prior
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register before executing normal Python imports, but don't retain a failed
    # module. These are fixed operator-configured files, never request paths.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class NativeModelBridge:
    """One native process owns every admitted call until backend cleanup ends.

    A disconnected client only loses delivery. It cannot cancel the native
    admission context or authorize another owner to unload the running model.
    HTTP callers never select a runtime path, executable graph or model token.
    """
    def __init__(self, settings, *, chat=None, image=None):
        config = settings.get("models", {})
        if config.get("enabled") is not True:
            raise PermissionError("Native model forwarding is not active")
        self._condition = threading.Condition()
        self._active = {}
        self._accepting = True
        self.sources = dict(config.get("sha256", {}))
        if chat is None or image is None:
            if sys.platform != "darwin":
                raise RuntimeError("Native Mac model admission is required")
            runtime = _load_source(config, "runtime_module", "_biomerte_native_model_runtime")
            chat_module = _load_source(config, "chat_module", "_biomerte_native_chat_backend")
            image_module = _load_source(config, "image_module", "_biomerte_native_comfy_backend")
            chat = chat_module.NativeChatBackend(runtime_factory=runtime.ModelRuntime)
            image = image_module.ComfyBackend(runtime_factory=runtime.ModelRuntime)
        self.chat, self.image = chat, image
        if image.model != IMAGE_MODEL:
            raise ValueError("Native image model identity mismatch")

    def status(self):
        with self._condition:
            return {"enabled":True, "accepting":self._accepting,
                    "active_requests":len(self._active),
                    "active":[{"request_id":key, "operation":value} for key,value in self._active.items()],
                    "runtime_domain":"native-mac",
                    "backends":["LM Studio", "ComfyUI"], "source_sha256":dict(self.sources)}

    def close(self):
        # No arbitrary generation deadline. The caller must drain before a
        # supervisor's external kill timeout; SIGKILL cannot be handled.
        with self._condition:
            self._accepting = False
            self._condition.wait_for(lambda: not self._active)

    def validate(self, operation, arguments):
        if operation not in OPERATIONS or not isinstance(arguments, dict):
            raise ValueError("Invalid native model operation")
        expected = ({"messages", "model"} if operation == "chat.infer" else
                    {"messages"} if operation == "image.generate" else set())
        if set(arguments) != expected:
            raise ValueError("Invalid native model arguments")
        if "messages" in arguments:
            messages = arguments["messages"]
            if not isinstance(messages, list) or not messages:
                raise ValueError("Invalid conversation")
            for index, value in enumerate(messages):
                if (not isinstance(value, dict) or set(value) != {"role", "content"}
                        or value["role"] not in ("system", "user", "assistant")
                        or (value["role"] == "system" and index != 0)
                        or not isinstance(value["content"], str)):
                    raise ValueError("Invalid conversation")

    def call(self, operation, arguments, progress=None, request_id=None):
        self.validate(operation, arguments)
        request_id = request_id or str(uuid.uuid4())
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError('Invalid model request identity')
        with self._condition:
            if not self._accepting:
                raise RuntimeError("Native model bridge is draining")
            if request_id in self._active:
                raise ValueError('Native model request is already active')
            self._active[request_id] = operation
        try:
            if operation == "chat.privacy":
                return self.chat.privacy_ready()
            if operation == "chat.models":
                return self.chat.catalog()
            if operation == "chat.infer":
                return self.chat.inference(arguments["messages"], arguments["model"])
            if operation == "image.ready":
                return self.image.ready()
            return self.image.generate(arguments["messages"], progress=progress)
        finally:
            with self._condition:
                self._active.pop(request_id, None)
                self._condition.notify_all()
