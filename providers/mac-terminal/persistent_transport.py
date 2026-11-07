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
    """The control RPC may have executed; retry only its original durable ID."""


class PersistentTransport:
    def __init__(self, origin, credentials, reply_timeout=90):
        self.origin = origin
        self.credentials = credentials
        # Match the existing HTTP transport's response horizon. This only
        # returns an unanswered control RPC to durable fallback; it neither
        # cancels nor places a lifetime limit on the capability/job it refers to.
        self.reply_timeout = reply_timeout
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.socket = None
        self.token = None
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
        item = {'ready': threading.Event()}
        with self.lock:
            if self.socket is not ws:
                raise ConnectionError('Persistent connection moving')
            self.pending[ident] = item
        try:
            with self.send_lock:
                ws.send(json.dumps({'type': 'invoke', 'id': ident, 'arguments': args}))
            ready = item['ready'].wait(self.reply_timeout)
            with self.lock:
                # A response may arrive at the timeout boundary.
                if not ready and not item['ready'].is_set():
                    raise ReplyPending('Persistent reply pending; durable request identity retained')
                if 'error' in item:
                    raise item['error']
                return {'result': item['result']}
        except ReplyPending:
            # Healthy heartbeats do not prove that this particular RPC replied.
            # Keep unrelated multiplexed calls alive. The caller's HTTP fallback
            # uses the same request_id, so a late mutation is never replayed as
            # a new logical operation.
            raise
        except Exception:
            self._disconnect(ws)
            raise
        finally:
            with self.lock:
                self.pending.pop(ident, None)
