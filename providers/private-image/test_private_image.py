import base64
import hashlib
import json
import secrets
import sys
import unittest
import uuid
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from comfy_backend import ComfyBackend
from image_protocol import SessionServer, public
from image_client import SessionClient
from private_image import store_envelope, store_bytes

class ArtifactRemote:
    def __init__(self):
        self.chunks = {}
    def call(self,name,args):
        assert name == "artifact.put"
        value = base64.b64decode(args["data"],validate=True)
        sha = hashlib.sha256(value).hexdigest()
        self.chunks[sha] = value
        return {"sha256":sha,"size":len(value)}

class ImageTests(unittest.TestCase):
    def session(self):
        identity,phone = Ed25519PrivateKey.generate(),Ed25519PrivateKey.generate()
        return SessionServer(identity,phone_identity=public(phone)),SessionClient(public(identity),phone)

    def test_encrypted_large_result_and_authenticated_chunks(self):
        server,client = self.session()
        client.accept(server.handle(client.hello,client.hello["id"],None))
        request = client.request([{"role":"user","content":"synthetic transport canary"}])
        pixels = base64.b64encode(secrets.token_bytes(700000)).decode()
        envelope = server.handle(request,request["id"],lambda messages:{"images":[{"data":pixels}]})
        remote = ArtifactRemote()
        packed = store_envelope(envelope,remote)["envelope"]
        self.assertEqual(packed["kind"],"artifact")
        self.assertNotIn(pixels,json.dumps(packed))
        encoded = b"".join(remote.chunks[x["sha256"]] for x in packed["chunks"])
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),packed["sha256"])
        recovered = json.loads(encoded)
        # Authentication rejects corrupted ciphertext before plaintext is accepted.
        damaged = dict(recovered); damaged["ct"] = base64.b64encode(b"X"+base64.b64decode(recovered["ct"])[1:]).decode()
        from image_protocol import open_message
        with self.assertRaises(Exception):
            open_message(client.traffic["response"],"response",client.sid,client.operation,client.thash,damaged)
        self.assertEqual(client.response(recovered)["images"][0]["data"],pixels)
        self.assertEqual(server.sessions,{})

    def test_chat_handshake_cannot_enter_image_service(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent.parent/"private-chat"))
        from session_client import SessionClient as ChatClient
        identity,phone = Ed25519PrivateKey.generate(),Ed25519PrivateKey.generate()
        server = SessionServer(identity,phone_identity=public(phone))
        chat = ChatClient(public(identity),phone)
        with self.assertRaises(Exception):
            server.handle(chat.hello,chat.hello["id"],None)

    def test_replay_rejected_without_second_generation(self):
        server,client = self.session()
        client.accept(server.handle(client.hello,client.hello["id"],None))
        request = client.request([{"role":"user","content":"test"}])
        server.handle(request,request["id"],lambda messages:{"images":[]})
        with self.assertRaises(Exception):
            server.handle(request,request["id"],lambda messages:self.fail("replayed"))

    def test_generic_artifact_bytes_roundtrip(self):
        remote=ArtifactRemote();image=b"\x89PNG\r\n\x1a\n"+secrets.token_bytes(400000)
        manifest=store_bytes(image,remote)
        self.assertEqual(b"".join(remote.chunks[x["sha256"]] for x in manifest["chunks"]),image)

    def test_successful_empty_cleanup_http_response(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        class Cleanup(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200); self.end_headers()
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1",0),Cleanup)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            backend = ComfyBackend()
            backend.url = "http://127.0.0.1:"+str(server.server_port)
            self.assertEqual(backend.api("/history",{"delete":["synthetic"]}),{})
            self.assertEqual(backend.api("/free",{"unload_models":True}),{})
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_user_graph_or_shell_is_never_accepted(self):
        backend=ComfyBackend()
        for spec in ({"prompt":"test","workflow":{}},{"prompt":"test","command":"rm -rf /"},{"prompt":"x","width":513},{"prompt":"x","steps":0}):
            with self.assertRaises(ValueError):
                backend.specification([{"role":"user","content":json.dumps(spec)}])

if __name__ == "__main__":
    unittest.main()
