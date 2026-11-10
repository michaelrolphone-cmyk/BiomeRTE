"""Persistent controller inbox, with the existing HTTP path as reconnect fallback.

Python 3.4 compatible; websocket-client is optional. No controller authority or
job receipts are held exclusively in a socket.
"""
import json
import threading
import time
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'transport_vendor'))
try:
    import queue
except ImportError:
    import Queue as queue


class ControllerLink:
    def __init__(self, origin, token, site_access=None):
        self.origin, self.token, self.site_access = origin, token, site_access
        self.jobs = queue.Queue()
        self.socket = None
        self.send_lock = threading.Lock()
        self.pending = {}
        self.lock = threading.Lock()
        self.last_http_poll = 0
        self.http_poll_interval = 2.0
        thread = threading.Thread(target=self.run)
        thread.daemon = True
        thread.start()

    def run(self):
        try:
            import websocket
        except ImportError:
            return
        while True:
            ws = None
            try:
                headers = ['Authorization: Bearer ' + self.token]
                if self.site_access:
                    headers.append('OAI-Sites-Authorization: Bearer ' + self.site_access)
                ws = websocket.create_connection(self.origin.replace('https://', 'wss://', 1) +
                    '/bridge/connect', header=headers, timeout=30, suppress_origin=True,
                    enable_multithread=True)
                with self.send_lock:
                    self.socket = ws
                thread = threading.Thread(target=self.heartbeat, args=(ws,))
                thread.daemon = True
                thread.start()
                while self.socket is ws:
                    raw = ws.recv()
                    if not raw:
                        break
                    self._receive(ws, json.loads(raw))
            except Exception:
                pass
            finally:
                with self.send_lock:
                    if self.socket is ws:
                        self.socket = None
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                with self.lock:
                    for item in self.pending.values():
                        if item['socket'] is ws:
                            item['ready'].set()
            time.sleep(2)

    def _receive(self, ws, frame):
        if frame.get('type') == 'job':
            # Local-only provenance, never part of the native request or result.
            # The gateway socket can acknowledge only jobs it delivered itself.
            frame['_delivery_socket'] = ws
            self.jobs.put(frame)
        elif frame.get('type') == 'accepted':
            with self.lock:
                item = self.pending.get(frame['id'])
                if item and item['socket'] is ws:
                    if frame.get('lease') == item['lease']:
                        item['accepted'] = frame['accepted']
                    elif 'lease' in frame:
                        # A delayed ACK for an earlier lease is not this result.
                        return
                    # Legacy gateways omit the lease. Wake into authenticated
                    # HTTP fallback rather than trusting an ambiguous ID-only ACK.
                    item['ready'].set()

    def heartbeat(self, ws):
        while self.socket is ws:
            time.sleep(10)
            try:
                with self.send_lock:
                    ws.send(json.dumps({'type': 'ping'}))
            except Exception:
                try:
                    ws.close()
                except Exception:
                    pass
                return

    def next(self):
        try:
            return self.jobs.get(timeout=.1)
        except queue.Empty:
            # A live socket/pong does not prove its delivery loop is progressing.
            # Periodically consult the same durable HTTP inbox, preserving job
            # IDs and leases. This is a poll cadence, not a command time limit.
            now = time.monotonic()
            if self.socket is None or now - self.last_http_poll >= self.http_poll_interval:
                self.last_http_poll = now
                return None
            return {'idle': True}

    def complete(self, body, delivery_socket=None):
        # HTTP claims and jobs delivered before reconnect use durable HTTP
        # completion immediately, rather than waiting for an impossible ACK.
        if delivery_socket is None:
            return None
        item = {'ready': threading.Event(), 'socket': delivery_socket, 'lease': body['lease']}
        try:
            with self.send_lock:
                if self.socket is not delivery_socket:
                    return None
                with self.lock:
                    # ACKs carry the job ID, not its lease. Never replace an
                    # outstanding waiter for another lease of that same job.
                    if body['id'] in self.pending:
                        return None
                    self.pending[body['id']] = item
                delivery_socket.send(json.dumps(dict(body, type='complete')))
            # Transport ACK timeout falls back to the same id/lease over HTTP.
            if not item['ready'].wait(15):
                return None
            if 'accepted' not in item:
                return None
            return {'accepted': item['accepted']}
        except Exception:
            return None
        finally:
            with self.lock:
                if self.pending.get(body['id']) is item:
                    self.pending.pop(body['id'], None)

