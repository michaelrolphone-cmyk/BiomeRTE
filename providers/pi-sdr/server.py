#!/usr/bin/env python3
"""Persistent receive-only SDR adapter; scan jobs outlive capability invocations."""
import fcntl
import hashlib
import json
import os
import socket
import socketserver
import sys
import time
import sqlite3
import main as radio

def serve(home, root):
    os.makedirs(home, exist_ok=True)
    lock = open(os.path.join(home, 'service.lock'), 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = os.path.join(home, 'service.sock')
    if os.path.exists(path):
        os.unlink(path)
    ctx = radio.start({'root': root})
    db = sqlite3.connect(os.path.join(home, 'receipts.sqlite'), isolation_level=None)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, signature TEXT, result TEXT, created REAL)')
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request = json.loads(self.rfile.readline().decode('utf8'))
            operation, inputs = request['operation'], request['inputs']
            signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
            old = db.execute('SELECT signature,result FROM receipts WHERE id=?', (operation,)).fetchone()
            if old:
                result = json.loads(old[1]) if old[0] == signature else {'ok':False,'error':{'message':'Operation ID reused with different inputs'}}
            else:
                try:
                    result = {'ok':True, 'data':radio.handle('call', inputs, ctx)}
                except Exception as exc:
                    result = {'ok':False,'error':{'type':type(exc).__name__,'message':str(exc)}}
                db.execute('INSERT INTO receipts VALUES(?,?,?,?)', (operation,signature,json.dumps(result),time.time()))
                db.execute('DELETE FROM receipts WHERE created<?', (time.time()-900,))
            self.wfile.write((json.dumps(result)+'\n').encode('utf8'))
    service = socketserver.UnixStreamServer(path, Handler)
    try:
        os.chmod(path, 0o600)
        service.serve_forever()
    finally:
        service.server_close()
        ctx.stop.set()
        db.close()

def call(home):
    request = json.load(sys.stdin)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(os.path.join(home, 'service.sock'))
        client.sendall((json.dumps(request)+'\n').encode('utf8'))
        with client.makefile('rb') as source:
            response = source.readline()
    if not response:
        raise RuntimeError('SDR service disconnected; reconcile the operation before retry')
    sys.stdout.write(response.decode('utf8'))

if __name__ == '__main__':
    if sys.argv[1] == 'serve':
        serve(sys.argv[2], sys.argv[3])
    else:
        call(sys.argv[2])
