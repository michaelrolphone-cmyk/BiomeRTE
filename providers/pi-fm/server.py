#!/usr/bin/env python3
"""Standalone FM daemon and BiomeRTE stdin adapter; Python 3.4+."""
import fcntl
import hashlib
import json
import os
import socket
import socketserver
import sqlite3
import sys
import time
import main as radio

class Dispatcher:
    def __init__(self, home, context):
        self.context = context
        self.db = sqlite3.connect(os.path.join(home, 'receipts.sqlite'), isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, signature TEXT, result TEXT, created REAL)')
    def execute(self, request):
        operation, inputs = request['operation'], request['inputs']
        if not isinstance(operation, str) or not operation or not isinstance(inputs, dict):
            raise ValueError('Expected operation ID and object inputs')
        signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        old = self.db.execute('SELECT signature,result FROM receipts WHERE id=?', (operation,)).fetchone()
        if old:
            if old[0] != signature:
                return {'ok': False, 'error': {'message': 'Operation ID reused with different inputs'}}
            return json.loads(old[1])
        inputs = dict(inputs)
        if inputs.get('action') == 'transmit' and 'request_id' not in inputs:
            inputs['request_id'] = hashlib.sha256(operation.encode()).hexdigest()
        try:
            result = {'ok': True, 'data': radio.handle('call', inputs, self.context)}
        except Exception as exc:
            result = {'ok': False, 'error': {'type': type(exc).__name__, 'message': str(exc)}}
        self.db.execute('INSERT INTO receipts VALUES(?,?,?,?)', (operation, signature, json.dumps(result), time.time()))
        self.db.execute('DELETE FROM receipts WHERE created<?', (time.time()-900,))
        return result

def serve(home, root):
    os.makedirs(home, exist_ok=True)
    lock = open(os.path.join(home, 'service.lock'), 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path = os.path.join(home, 'service.sock')
    if os.path.exists(path):
        os.unlink(path)
    context = radio.start({'root': root, 'audio_root': '/home/pi'})
    dispatcher = Dispatcher(home, context)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                result = dispatcher.execute(json.loads(self.rfile.readline().decode('utf8')))
            except Exception as exc:
                result = {'ok': False, 'error': {'type': type(exc).__name__, 'message': str(exc)}}
            self.wfile.write((json.dumps(result)+'\n').encode('utf8'))
    service = socketserver.UnixStreamServer(path, Handler)
    os.chmod(path, 0o600)
    try:
        service.serve_forever()
    finally:
        service.server_close()
        dispatcher.db.close()

def call(home):
    request = json.load(sys.stdin)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(os.path.join(home, 'service.sock'))
        client.sendall((json.dumps(request)+'\n').encode('utf8'))
        with client.makefile('rb') as source:
            response = source.readline()
    if not response:
        raise RuntimeError('FM daemon disconnected; reconcile operation before retry')
    sys.stdout.write(response.decode('utf8'))

if __name__ == '__main__':
    if sys.argv[1] == 'serve':
        serve(sys.argv[2], sys.argv[3])
    else:
        call(sys.argv[2])
