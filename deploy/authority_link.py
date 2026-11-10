"""Read-only authority inspection link; Python 3.4+, no command execution."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'transport_vendor'))


def authority_key(secret, authority):
    return hmac.new(secret.encode(), ('authority-inspect:' + authority).encode(),
                    hashlib.sha256).hexdigest()


def configuration(home):
    with open(os.path.join(home, 'node.json')) as source:
        value = json.load(source)
    node, secret = value['node'], value.get('stream_router_secret', '')
    public = urllib.parse.urlsplit(value.get('stream_gateway_url', ''))
    if (not re.match('^[a-f0-9]{64}$', node) or len(secret) < 32 or
            public.scheme != 'wss' or not public.hostname or public.username or
            public.password or public.path not in ('', '/') or public.query or public.fragment):
        raise ValueError('Private stream authority configuration required')
    return dict(node=node, secret=secret,
                url=urllib.parse.urlunsplit(('wss', public.netloc,
                    '/authority/' + node + '/connect', '', '')),
                port=value.get('port', 8740), admin_token=value['admin_token'])


def inspect(config, request):
    if (not isinstance(request, dict) or set(request) != {'type', 'id', 'authority', 'session'}
            or request['type'] != 'inspect' or request['authority'] != config['node']
            or not isinstance(request['id'], str) or not re.match('^[a-f0-9]{32}$', request['id'])
            or not isinstance(request['session'], str) or not re.match('^[a-f0-9]{32}$', request['session'])):
        raise ValueError('Invalid authority inspection request')
    reply = dict(type='inspect_result', id=request['id'], status=503)
    body = json.dumps(dict(operation='stream.inspect',
                          arguments={'session':request['session']}, realm='site',
                          id='authority-' + request['id'])).encode()
    native = urllib.request.Request('http://127.0.0.1:' + str(config['port']) + '/v1/call',
        body, {'Content-Type':'application/json',
               'Authorization':'Bearer ' + config['admin_token']})
    try:
        with urllib.request.urlopen(native, timeout=4) as response:
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError('Authority reply too large')
            envelope = json.loads(raw.decode())
            if not isinstance(envelope, dict) or set(envelope) != {'result'}:
                raise ValueError('Invalid native authority response envelope')
            grant = envelope['result']
            if (not isinstance(grant, dict) or grant.get('authority') != config['node']
                    or grant.get('id') != request['session']):
                raise ValueError('Authority reply binding mismatch')
            reply.update(status=200, grant=grant)
    except urllib.error.HTTPError as error:
        # Confirmed revocation remains distinct from temporary unavailability.
        reply['status'] = error.code if error.code in (400, 403, 404, 409, 429) else 503
    except (OSError, ValueError):
        pass
    return reply


class Link:
    def __init__(self, config):
        self.config = config
        self.stop = threading.Event()
        self.socket = None

    def close(self):
        self.stop.set()
        socket = self.socket
        if socket is not None:
            socket.close()

    def connected(self, socket):
        lock = threading.Lock()
        slots = threading.BoundedSemaphore(32)
        pool = ThreadPoolExecutor(max_workers=4)

        def send(value):
            with lock:
                socket.send(json.dumps(value, separators=(',', ':')))

        def answer(value):
            try:
                send(inspect(self.config, value))
            except (OSError, ValueError):
                pass
            finally:
                slots.release()

        try:
            while not self.stop.is_set():
                raw = socket.recv()
                if not raw:
                    break
                if not isinstance(raw, str) or len(raw) > 1024:
                    raise ValueError('Invalid authority request size')
                value = json.loads(raw)
                # Validate before admitting a request to the bounded executor.
                if (not isinstance(value, dict) or set(value) != {'type','id','authority','session'}
                        or value.get('type') != 'inspect' or value.get('authority') != self.config['node']
                        or not isinstance(value.get('id'), str)
                        or not re.match('^[a-f0-9]{32}$', value['id'])
                        or not isinstance(value.get('session'), str)
                        or not re.match('^[a-f0-9]{32}$', value['session'])):
                    raise ValueError('Invalid authority envelope')
                if not slots.acquire(False):
                    send(dict(type='inspect_result', id=value['id'], status=429))
                    continue
                try:
                    pool.submit(answer, value)
                except BaseException:
                    slots.release()
                    raise
        finally:
            # All work is read-only. Never publish an old reply on a new socket.
            pool.shutdown(wait=True)

    def run(self):
        import websocket
        while not self.stop.is_set():
            socket = None
            try:
                socket = websocket.create_connection(self.config['url'],
                    header=['Authorization: Authority ' + authority_key(
                        self.config['secret'], self.config['node'])],
                    timeout=20, suppress_origin=True, enable_multithread=True)
                self.socket = socket
                self.connected(socket)
            except Exception as error:
                sys.stderr.write('Authority link reconnect: ' + type(error).__name__ + '\n')
                sys.stderr.flush()
            finally:
                self.socket = None
                if socket is not None:
                    socket.close()
            self.stop.wait(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    link = Link(configuration(args.home))
    signal.signal(signal.SIGTERM, lambda *unused: link.close())
    signal.signal(signal.SIGINT, lambda *unused: link.close())
    link.run()


if __name__ == '__main__':
    main()
