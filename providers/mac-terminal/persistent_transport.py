"""Multiplexed outbound Biome transport; durable RPC IDs survive reconnects.

Only public metadata and encrypted private-chat envelopes pass through here.
No request, response or credentials are logged or written by this module.
"""
import json
import threading
import time
import uuid
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'transport_vendor'))


class ReplyPending(ConnectionError):
    """The RPC may have reached the controller. Keep its original request ID."""


class PersistentTransport:
    def __init__(self, origin, credentials, reply_timeout=90):
        self.origin = origin
        self.credentials = credentials
        # Match the HTTP response horizon. Only the transport wait is bounded.
        self.reply_timeout = reply_timeout
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.socket = None
        self.token = None
        self.draining = False
        self.pending = {}

    def _connect(self):
        import websocket
        credential = self.credentials()
        token = credential['access_token']
        with self.lock:
            if self.socket is not None and self.token == token:
                return self.socket
            self._disconnect()
            ws = websocket.create_connection(
                self.origin.replace('https://', 'wss://', 1) + '/api/provider/connect',
                header=['Authorization: Bearer ' + token], timeout=30,
                suppress_origin=True, enable_multithread=True)
            # Read timeout is a liveness check, not a command lifetime limit.
            self.socket, self.token = ws, token
            threading.Thread(target=self._receive, args=(ws,), daemon=True).start()
            threading.Thread(target=self._heartbeat, args=(ws,), daemon=True).start()
            return ws

    def _disconnect(self, expected=None):
        with self.lock:
            if expected is not None and self.socket is not expected:
                return
            ws, self.socket = self.socket, None
            self.draining = False
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            for item in self.pending.values():
                item['error'] = ConnectionError('Persistent connection interrupted')
                item['ready'].set()

    def _receive(self, ws):
        try:
            while self.socket is ws:
                data = ws.recv()
                if not data:
                    break
                frame = json.loads(data)
                if frame.get('type') != 'reply':
                    continue
                with self.lock:
                    item = self.pending.get(frame.get('id'))
                    if item:
                        item['result'] = frame['result']
                        item['ready'].set()
        except Exception:
            pass
        finally:
            self._disconnect(ws)

    def _heartbeat(self, ws):
        while self.socket is ws:
            time.sleep(10)
            try:
                with self.send_lock:
                    ws.send(json.dumps({'type': 'ping'}))
            except Exception:
                self._disconnect(ws)
                return

    def invoke(self, args):
        ws = self._connect()
        ident = uuid.uuid4().hex
        with self.lock:
            if self.socket is not ws:
                raise ConnectionError('Persistent connection moving')
            if self.draining:
                raise ReplyPending('Persistent connection is retiring an old inbox poll')
            item = {'ready': threading.Event()}
            self.pending[ident] = item
        try:
            with self.send_lock:
                ws.send(json.dumps({'type': 'invoke', 'id': ident, 'arguments': args}))
            ready = item['ready'].wait(self.reply_timeout)
            with self.lock:
                # Check again under the receive lock for an ACK at the boundary.
                if not ready and not item['ready'].is_set():
                    if args.get('operation') == 'provider.poll' and self.socket is ws:
                        # The gateway can hold an empty inbox poll until the socket
                        # closes. Drain other replies before retiring that poll.
                        self.draining = True
                    raise ReplyPending('Persistent reply pending; durable request identity retained')
                if 'error' in item:
                    raise item['error']
                return {'result': item['result']}
        except ReplyPending:
            # Socket pongs do not prove this RPC completed. Let the caller use
            # its existing HTTP fallback, keeping unrelated socket calls alive.
            raise
        except Exception:
            self._disconnect(ws)
            raise
        finally:
            with self.lock:
                self.pending.pop(ident, None)
                if self.socket is ws and self.draining and not self.pending:
                    self._disconnect(ws)
