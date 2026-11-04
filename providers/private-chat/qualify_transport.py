"""Measure the deployed encrypted transport using disposable in-memory identities.

This never reads the phone's key, alters pairing, or persists model/chat content.
Only timings, model count and boolean verification results are printed.
"""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.request import Request, urlopen
from cryptography.hazmat.primitives.asymmetric import ed25519
from session_client import SessionClient
from session_protocol import SessionServer, public, seal
from private_chat import PrivateWorker, Remote, gateway, handle_request


def main():
    os.umask(0o077)
    signing = ed25519.Ed25519PrivateKey.generate()
    phone = ed25519.Ed25519PrivateKey.generate()
    server = SessionServer(signing, phone_identity=public(phone), request_handler=handle_request)
    ident = uuid.uuid4().hex
    resource = 'privatechat.qualify.' + ident
    provider = 'qualify.' + ident
    site = json.loads((Path.home()/'.local/share/biome-private-chat/manifest.json').read_text())['site']
    offer = gateway.invoke('cap.offer', dict(provider=provider, contract='chat.private.exchange:2',
        resource=resource, capacity=1, service='private-chat'))
    stopped = threading.Event()
    errors = []
    with tempfile.TemporaryDirectory(prefix='biome-transport-qualify-') as directory:
        def work():
            worker = PrivateWorker(str(Path(directory)/'worker.sqlite'), Remote(dict(provider=provider,site=site)),
                [dict(contract='chat.private.exchange:2',resource=resource)],None,
                handler=lambda action: {'envelope':server.handle(action['inputs']['envelope'],action['id'],None)})
            try:
                while not stopped.is_set():
                    worker.once()
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                worker.db.close()
        thread = threading.Thread(target=work,daemon=True)
        thread.start()

        def exchange(envelope):
            credential = gateway.credentials()
            payload=json.dumps(dict(contract='chat.private.exchange:2',resource=resource,envelope=envelope))
            start=time.monotonic()
            with tempfile.TemporaryDirectory(prefix='biome-qualify-http-') as temporary:
                cfg=Path(temporary)/'curl.conf'
                cfg.write_text('header = "Authorization: Bearer '+credential['access_token']+'"\nheader = "Content-Type: application/json"\n')
                process=subprocess.Popen(['/usr/bin/curl','--config',str(cfg),'-fsS','-N','--data-binary','@-',gateway.ORIGIN+'/api/exchange'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                process.stdin.write(payload);process.stdin.close()
                for line in process.stdout:
                    frame=json.loads(line)
                    if frame['type']=='result':
                        status=frame['status']
                        if status['state']!='succeeded':raise RuntimeError('Qualification command did not succeed')
                        if process.wait()!=0:raise RuntimeError('Qualification transport failed')
                        return status['result']['envelope'],time.monotonic()-start
                    if frame['type']=='error':raise RuntimeError('Qualification connection interrupted')
                if process.wait()!=0:raise RuntimeError('Qualification transport failed')
            raise RuntimeError('Qualification response missing')

        try:
            client=SessionClient(public(signing),phone)
            hello,handshake=exchange(client.hello)
            client.accept(hello)
            client.operation=str(uuid.uuid4())
            request=seal(client.traffic['request'],'request',client.sid,client.operation,client.thash,{'action':'models'})
            reply,listing=exchange(request)
            result=client.response(reply)
            # Same encrypted envelope recovers the retained result after release.
            retry,resume=exchange(request)
            assert retry==reply
            print(json.dumps(dict(handshake_seconds=round(handshake,3),models_seconds=round(listing,3),
                total_seconds=round(handshake+listing,3),resume_seconds=round(resume,3),
                model_count=len(result['models']),same_encrypted_reply=True,
                persistent_provider_connected=gateway.transport_active(),worker_errors=errors)))
        finally:
            stopped.set()
            # Closing the transport wakes its held inbox read for clean shutdown.
            if gateway._persistent is not None:gateway._persistent._disconnect()
            thread.join(5)
            if not thread.is_alive():gateway.invoke('cap.delete',{'offer':offer['id']})


if __name__=='__main__':
    main()
