"""Private workers over the existing TLS HostIO boundary; synthetic models only."""
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"providers/private-chat"),
               str(ROOT/"providers/private-image"), str(ROOT/"providers/model-runtime")]
from biomerte.host_io import HostIO, make_server, make_servers
from biomerte.host_io_client import HostIOClient, PROTOCOL
from biomerte.model_host_io import NativeModelBridge, ModelHostClient, ModelHostError, IMAGE_MODEL
from model_service import NativeChatBackend
from model_runtime import ModelRuntime
from comfy_backend import ComfyBackend
from session_protocol import SessionServer as ChatServer, public
from session_client import SessionClient as ChatClient
from image_protocol import SessionServer as ImageServer
from image_client import SessionClient as ImageClient
from private_image import store_envelope
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class Routes:
    def __init__(self, path): pass
    def list(self): return []
    def get(self, device_id): return None
class RouteModule:
    DeviceRoutes = Routes


class Models:
    def __init__(self):
        self.events = []
    def prepare(self, kind, model, progress=None):
        self.events.append(("prepare", kind, model, os.getpid()))
        return "exact-instance" if kind == "llm" else None
    def release(self, kind):
        self.events.append(("release", kind))


class ChatHTTP:
    def __init__(self):
        self.bodies = []
        self.returned_model = "exact-instance"
    def open(self, request):
        body = json.loads(request.data)
        self.bodies.append(body)
        return io.StringIO(json.dumps({"model":self.returned_model,
            "choices":[{"finish_reason":"stop","message":{"content":"synthetic answer"}}],
            "usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}))


class Image(ComfyBackend):
    def __init__(self, runtime_factory):
        super().__init__(runtime_factory=runtime_factory)
        self.entered = threading.Event()
        self.finish = threading.Event()
        self.finish.set()
        self.cleaned = threading.Event()
        self.events = []
        self.graphs = []
        self.png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))*3100
    def api(self, path, data=None, method=None):
        self.events.append((path, method))
        if path == "/object_info":
            return {"BiomeMemoryImage":{},
                    "UnetLoaderGGUF":{"input":{"required":{"unet_name":[[self.model]]}}}}
        if path == "/prompt":
            self.graphs.append(data["prompt"])
            self.entered.set()
            return {"prompt_id":"synthetic-prompt"}
        if path == "/history/synthetic-prompt":
            if not self.finish.wait(10):
                raise RuntimeError("fixture release was not signaled")
            return {"synthetic-prompt":{"status":{"completed":True,"status_str":"success"}}}
        if path.startswith("/biome/private-image/") and method != "DELETE":
            return {"images":[base64.b64encode(self.png).decode()]}
        if path == "/free":
            self.cleaned.set()
        return {}
    def watch_progress(self, client, prompt_id, stopped, publish):
        publish({"stage":"sampling","step":1,"total":2})
        publish({"stage":"sampling","step":2,"total":2})


class ArtifactRemote:
    def __init__(self): self.chunks = {}
    def call(self, operation, arguments):
        if operation != "artifact.put": raise AssertionError(operation)
        raw = base64.b64decode(arguments["data"], validate=True)
        sha = hashlib.sha256(raw).hexdigest()
        self.chunks[sha] = raw
        return {"sha256":sha,"size":len(raw)}


class TLSModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certdir = tempfile.TemporaryDirectory()
        root = Path(cls.certdir.name)
        cls.cert, cls.key = root/"cert.pem", root/"key.pem"
        cfg = root/"openssl.cnf"
        cfg.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                       "[dn]\nCN=localhost\n[ext]\n"
                       "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
                       "basicConstraints=critical,CA:TRUE\n"
                       "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
                       "extendedKeyUsage=serverAuth\n")
        subprocess.run(["/usr/bin/openssl","req","-x509","-newkey","rsa:2048","-nodes",
                        "-keyout",str(cls.key),"-out",str(cls.cert),"-days","1","-config",str(cfg)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.key.chmod(0o600)
    @classmethod
    def tearDownClass(cls):
        cls.certdir.cleanup()
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = self.root/"token"
        self.token.write_text("synthetic-host-credential-"*3)
        self.token.chmod(0o600)
        (self.root/"routes").touch()
        self.state = self.root/"chat"
        self.state.mkdir()
        (self.state/"privacy.json").write_text(json.dumps({
            "lmstudio_request_logging_disabled":True,"qualification":"verified"}))
        logs = self.root/".lmstudio/.internal"
        logs.mkdir(parents=True)
        self.logs = logs/"http-server-config.json"
        self.logs.write_text(json.dumps({"logSensitiveData":False,"logIncomingTokens":False}))
        self.models = Models()
        self.runtime = ModelRuntime(self.root/"admission", self.models)
        self.http = ChatHTTP()
        self.chat = NativeChatBackend(home=self.root, state=self.state, lm=self.root/"lm",
                                      runtime_factory=lambda:self.runtime, opener=self.http)
        self.image = Image(lambda:self.runtime)
        self.bridge = NativeModelBridge({"models":{"enabled":True}},
                                         chat=self.chat, image=self.image)
        self.settings = dict(host_id="registered-native-mac", token_file=str(self.token),
            device_routes=str(self.root/"routes"), vision_directory=str(self.root),
            listen="127.0.0.1", port=0, models={"enabled":True},
            tls={"cert_file":str(self.cert),"key_file":str(self.key)})
        self.host = HostIO(self.settings, routes_module=RouteModule, clock_id="fixture-boot")
        self.host._model = self.bridge
        self.server = make_server(self.settings,self.host)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = HostIOClient(dict(url="https://127.0.0.1:"+str(self.server.server_port),
            ca_file=str(self.cert), token_file=str(self.token), host_id="registered-native-mac"))
        self.worker_backend = ModelHostClient(self.client)
    def tearDown(self):
        self.image.finish.set()
        self.host.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()
    def session(self, kind):
        key, phone = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        if kind == "chat":
            server = ChatServer(key, phone_identity=public(phone),
                request_handler=lambda body:self.worker_backend.inference(body["messages"],body["model"]))
            client = ChatClient(public(key),phone)
        else:
            server = ImageServer(key,phone_identity=public(phone))
            client = ImageClient(public(key),phone)
        client.accept(server.handle(client.hello,client.hello["id"],None))
        return server,client
    def test_tls_auth_and_pinned_host_reject_before_model_work(self):
        plain = HostIOClient(dict(url="http://127.0.0.1:1", token_file=str(self.token),
                                  host_id="registered-native-mac", allow_local_http=True))
        with self.assertRaises(ValueError): ModelHostClient(plain)
        wrong = HostIOClient(dict(self.client.config, host_id="other-host"))
        with self.assertRaises(HTTPError) as error:
            wrong.model_call("image.generate",{"messages":[{"role":"user","content":"synthetic"}]})
        self.assertEqual(error.exception.code,400)
        bad = self.root/"bad-token";bad.write_text("incorrect-token")
        unauthorized = HostIOClient(dict(self.client.config,token_file=str(bad)))
        with self.assertRaises(HTTPError) as error:
            unauthorized.model_call("chat.privacy",{})
        self.assertEqual(error.exception.code,401)
        self.assertFalse(self.models.events)
        self.assertFalse(self.image.graphs)
        untrusted = HostIOClient(dict(self.client.config,ca_file=None))
        with self.assertRaises(OSError):untrusted.model_call("chat.privacy",{})
    def test_compatible_listeners_share_backend_and_credentials_require_tls(self):
        settings = dict(self.settings)
        settings.pop("tls")
        settings["tls_listener"] = {"listen":"127.0.0.1","port":0,"tls":self.settings["tls"]}
        servers = make_servers(settings,self.host)
        threads = [threading.Thread(target=s.serve_forever,daemon=True) for s in servers]
        for thread in threads:thread.start()
        try:
            plain = HostIOClient(dict(url="http://127.0.0.1:"+str(servers[0].server_port),
                allow_local_http=True,token_file=str(self.token),host_id=self.host.host_id))
            tls = HostIOClient(dict(self.client.config,url="https://127.0.0.1:"+str(servers[1].server_port)))
            self.assertEqual(plain.call("clock")["clock_id"],tls.call("clock")["clock_id"])
            for operation in ("gateway.invoke","gitlab.token"):
                with self.assertRaises(HTTPError) as error:
                    plain.call(operation,{})
                self.assertEqual(error.exception.code,403)
            self.assertEqual(tls.model_call("chat.privacy",{}),{"ready":True})
            self.assertEqual(tls.call("model.status")["source_sha256"],self.bridge.status()["source_sha256"])
            with self.assertRaises(HTTPError) as error:
                plain._request("/v1/host-io/model",dict(protocol=PROTOCOL,host_id=self.host.host_id,
                    request_id=str(uuid.uuid4()),operation="chat.privacy",arguments={}))
            self.assertEqual(error.exception.code,403)
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(2)

    def test_chat_session_keeps_exact_selection_privacy_and_stateless_inference(self):
        server, client = self.session("chat")
        from session_protocol import seal
        client.operation = str(uuid.uuid4())
        request = seal(client.traffic["request"], "request", client.sid, client.operation,
                       client.thash, {"messages":[{"role":"user","content":"PRIVATE_CANARY_PROMPT"}],
                                      "model":"requested-key"})
        response = server.handle(request,request["id"],None)
        result = client.response(response)
        self.assertEqual(result["model"],"exact-instance")
        self.assertEqual(result["diagnostics"]["requested_model"],"requested-key")
        self.assertTrue(result["diagnostics"]["model_verified"])
        self.assertEqual(self.http.bodies[0]["model"],"exact-instance")
        self.assertIs(self.http.bodies[0]["store"],False)
        self.assertEqual(self.models.events[0][:3],("prepare","llm","requested-key"))
        self.assertEqual(self.models.events[0][3],os.getpid())
        self.assertEqual(self.runtime.state(),[])
        self.assertEqual(server.sessions,{})
        with self.assertRaises(ValueError):server.handle(request,request["id"],None)
        self.assertEqual(len(self.http.bodies),1)
        for path in self.root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(b"PRIVATE_CANARY_PROMPT",data)
                self.assertNotIn(b"synthetic answer",data)
    def test_native_logging_qualification_is_checked_on_every_request(self):
        self.worker_backend.privacy_ready()
        self.logs.write_text(json.dumps({"logSensitiveData":True,"logIncomingTokens":False}))
        with self.assertRaises(ModelHostError) as error:
            self.worker_backend.inference([{"role":"user","content":"secret-canary"}],"requested-key")
        self.assertEqual(error.exception.public_code,"private_inference_unavailable")
        self.assertFalse(self.models.events)
        self.assertNotIn("secret-canary",str(error.exception))
    def test_exact_model_response_mismatch_is_preserved(self):
        self.http.returned_model = "different-model"
        with self.assertRaises(ModelHostError) as error:
            self.worker_backend.inference([{"role":"user","content":"synthetic"}],"requested-key")
        self.assertEqual(error.exception.public_code,"model_response_mismatch")
        self.assertEqual(self.models.events[-1],("release","llm"))
        self.assertEqual(self.runtime.state(),[])
    def test_image_encrypted_progress_large_artifacts_and_cleanup(self):
        self.assertTrue(self.worker_backend.ready()["ready"])
        server,client = self.session("image")
        request = client.request([{"role":"user","content":"private image canary"}])
        progress = []
        response = server.handle(request,request["id"],self.worker_backend.generate,
                                 progress=lambda event:progress.append(event))
        remote = ArtifactRemote()
        packed = store_envelope(response,remote)["envelope"]
        self.assertEqual(packed["kind"],"artifact")
        encoded = b"".join(remote.chunks[row["sha256"]] for row in packed["chunks"])
        self.assertTrue(progress)
        from image_protocol import open_message
        updates = [open_message(client.traffic["response"],"progress",client.sid,
                               client.operation,client.thash,value) for value in progress]
        self.assertTrue(any(value.get("stage")=="sampling" for value in updates))
        self.assertEqual([v["seq"] for v in updates],list(range(1,len(updates)+1)))
        result = client.response(json.loads(encoded))
        self.assertEqual(base64.b64decode(result["images"][0]["data"]),self.image.png)
        self.assertEqual(result["model"],IMAGE_MODEL)
        self.assertNotIn("private image canary",json.dumps(progress))
        self.assertTrue(self.image.cleaned.is_set())
        self.assertTrue(any(path=="/history" for path,method in self.image.events))
        self.assertTrue(any(method=="DELETE" for path,method in self.image.events))
        self.assertEqual(self.models.events[-1],("release","image"))
        self.assertEqual(self.runtime.state(),[])
        self.assertEqual(server.sessions,{})
    def test_disconnect_retains_fifo_native_custody_and_shutdown_drains(self):
        self.image.finish.clear()
        request_id = str(uuid.uuid4())
        body = dict(protocol=PROTOCOL,host_id=self.host.host_id,request_id=request_id,
                    operation="image.generate",arguments={"messages":[{"role":"user","content":"synthetic"}]})
        response = self.client._request("/v1/host-io/model",body,timeout=None)
        self.assertEqual(json.loads(response.readline())["type"],"start")
        self.assertTrue(self.image.entered.wait(2))
        response.close()
        outcomes = []
        def second():
            try:outcomes.append(self.worker_backend.inference([{"role":"user","content":"next"}],"next-model"))
            except Exception as error:outcomes.append(error)
        next_call = threading.Thread(target=second)
        next_call.start()
        deadline = time.monotonic()+4
        while len(self.runtime.state()) != 2 and time.monotonic()<deadline:
            time.sleep(.02)
        rows = self.runtime.state()
        self.assertEqual([row["kind"] for row in rows],["image","llm"])
        self.assertEqual([row["phase"] for row in rows],["running","queued"])
        self.assertTrue(all(row["pid"]==os.getpid() for row in rows))
        self.assertEqual(len([e for e in self.models.events if e[0]=="prepare"]),1)
        state = self.client.call("model.status")
        self.assertEqual(state["active_requests"],2)
        # A long generation remains healthy by this worker's exact live request,
        # even when its last gateway poll is older than the normal health window.
        health_config = self.root/"client.json";health_config.write_text(json.dumps(self.client.config))
        model_health = self.root/"active.json";model_health.write_text(json.dumps({
            "request_id":request_id,"worker_pid":os.getpid(),"started_at":time.time()-1000}))
        spec = importlib.util.spec_from_file_location("private_worker_health",ROOT/"deploy/workers/private/health.py")
        health = importlib.util.module_from_spec(spec);spec.loader.exec_module(health)
        with patch.dict(os.environ,{"BIOME_WORKER_HEALTH":str(self.root/"no-poll"),
                "BIOME_PRIVATE_MODEL_HEALTH":str(model_health),
                "BIOME_MODEL_HOST_IO_CONFIG":str(health_config)}):
            self.assertTrue(health.healthy())
        self.assertIn(request_id,[row["request_id"] for row in state["active"]])
        drained = threading.Event()
        closer = threading.Thread(target=lambda:(self.bridge.close(),drained.set()))
        closer.start()
        self.assertFalse(drained.wait(.05))
        self.image.finish.set()
        next_call.join(4);closer.join(4)
        self.assertFalse(next_call.is_alive())
        self.assertTrue(drained.is_set())
        self.assertTrue(self.image.cleaned.is_set())
        self.assertEqual([e[:2] for e in self.models.events],
                         [("prepare","image"),("release","image"),("prepare","llm"),("release","llm")])
        self.assertEqual(outcomes[0]["model"],"exact-instance")
        self.assertEqual(self.bridge.status()["active_requests"],0)
    def test_progress_delivery_failure_never_cancels_generation(self):
        def unavailable(value):raise RuntimeError("synthetic delivery failure")
        result = self.worker_backend.generate([{"role":"user","content":"synthetic"}],progress=unavailable)
        self.assertEqual(result["model"],IMAGE_MODEL)
        self.assertTrue(self.image.cleaned.is_set())
        self.assertEqual(self.runtime.state(),[])
    def test_no_automatic_retry_after_truncated_model_stream(self):
        calls = []
        def opener(request, timeout=None):
            calls.append((request,timeout))
            body = json.loads(request.data)
            start = dict(type="start",protocol=PROTOCOL,host_id=self.host.host_id,
                         operation=body["operation"],request_id=body["request_id"],clock_id="fixture-boot")
            return io.BytesIO(json.dumps(start).encode()+b"\n")
        client = HostIOClient(self.client.config,opener=opener)
        with self.assertRaises(ValueError):
            client.model_call("chat.privacy",{})
        self.assertEqual(len(calls),1)
        self.assertIsNone(calls[0][1])
    def test_health_metadata_is_ephemeral_and_never_contains_payload(self):
        receipt = self.root/"model-health.json"
        seen = []
        def opener(request, timeout=None):
            value = json.loads(request.data)
            seen.append(json.loads(receipt.read_text()))
            start = dict(type="start",protocol=PROTOCOL,host_id=self.host.host_id,
                         operation=value["operation"],request_id=value["request_id"],clock_id="fixture-boot")
            return io.BytesIO(json.dumps(start).encode()+b"\n")
        client = HostIOClient(self.client.config,opener=opener)
        with patch.dict(os.environ,{"BIOME_PRIVATE_MODEL_HEALTH":str(receipt)}):
            with self.assertRaises(ValueError):
                client.model_call("chat.infer",{"messages":[{"role":"user","content":"private-prompt"}],"model":"requested-key"})
        self.assertFalse(receipt.exists())
        self.assertEqual(set(seen[0]),{"request_id","worker_pid","started_at"})
        self.assertNotIn("private-prompt",json.dumps(seen))


class SourcePinTests(unittest.TestCase):
    def test_disabled_or_unpinned_native_source_fails_closed(self):
        with self.assertRaises(PermissionError):
            NativeModelBridge({})
        with patch("sys.platform","darwin"):
            with self.assertRaises(ValueError):
                NativeModelBridge({"models":{"enabled":True,"runtime_module":"/not-a-source"}})


if __name__ == "__main__":
    unittest.main()
