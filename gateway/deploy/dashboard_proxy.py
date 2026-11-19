"""Narrow dashboard JSON relay. Credentials and response files stay on this host."""
import hashlib
import json
import os
import re
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

CHUNK_BYTES = 128 * 1024


class NoDashboardRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def dashboard_cache():
    path = os.path.expanduser('~/.local/share/biomerte-gateway/dashboard-cache')
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def dispatch_dashboard(name, args, local, conf):
    if name == 'biome_dashboard_definition':
        from dashboard_read import read_dashboard_definition
        return {'content': [{'type': 'text', 'text': json.dumps(read_dashboard_definition(args, conf))}]}
    cache = dashboard_cache()
    if name == 'biome_dashboard_chunk':
        import base64
        key, index = args.get('sha256', ''), args.get('index')
        if not re.match(r'^[0-9a-f]{64}$', key) or type(index) is not int or index < 0:
            raise ValueError('Invalid dashboard chunk')
        with open(os.path.join(cache, key + '.json'), 'rb') as handle:
            handle.seek(index * CHUNK_BYTES)
            data = handle.read(CHUNK_BYTES)
        if not data:
            raise ValueError('Dashboard chunk missing or expired')
        value = {'base64': base64.b64encode(data).decode('ascii'),
                 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    elif name == 'biome_dashboard_call':
        now = time.time()
        for entry in os.listdir(cache):
            path = os.path.join(cache, entry)
            try:
                if now - os.stat(path).st_mtime > 3600:
                    os.unlink(path)
            except OSError:
                pass
        # Exact local JSON bus only. No caller-selected URL, method, or headers.
        req = Request(local + '/v1/call', data=json.dumps(args).encode('utf-8'),
                      headers={'Content-Type': 'application/json',
                               'Authorization': 'Bearer ' + conf['admin_token']})
        try:
            response = build_opener(NoDashboardRedirect).open(req, timeout=30)
            status = response.getcode()
        except HTTPError as exc:
            response, status = exc, exc.code
        fd, temporary = tempfile.mkstemp(prefix='response-', dir=cache)
        digest, chunks, size = hashlib.sha256(), [], 0
        try:
            with os.fdopen(fd, 'wb') as target:
                with response:
                    while True:
                        data = response.read(CHUNK_BYTES)
                        if not data:
                            break
                        target.write(data)
                        digest.update(data)
                        size += len(data)
                        chunks.append({'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
            if len(chunks) <= 1:
                with open(temporary, 'rb') as handle:
                    value = {'status': status, 'body': json.loads(handle.read().decode('utf-8'))}
                os.unlink(temporary)
            else:
                key = digest.hexdigest()
                os.replace(temporary, os.path.join(cache, key + '.json'))
                value = {'status': status, 'transport': {'sha256': key, 'size': size, 'chunks': chunks}}
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
    else:
        raise ValueError('Unknown dashboard operation')
    return {'content': [{'type': 'text', 'text': json.dumps(value)}]}
