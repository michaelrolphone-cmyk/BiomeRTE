"""Existing controller gateway transport; credentials stay in existing files."""
import json
import os
import ssl
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler
from .store import Fault

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = build_opener(NoRedirect, HTTPSHandler(context=ssl.create_default_context()))

def fetch(url, payload=None, token=None, timeout=18, site_access=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {'User-Agent':'BiomeRTE-Controller-Link/1.0','Content-Type':'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if site_access:
        headers['OAI-Sites-Authorization'] = 'Bearer ' + site_access
    req = Request(url, data=data, headers=headers)
    with opener.open(req, timeout=timeout) as res:
        body = res.read()
    return json.loads(body.decode('utf-8'))

def read_secret(path):
    with open(path) as handle:
        token = handle.read().strip()
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Invalid dedicated bridge key')
    return token

def credentials(origin, key_file):
    origin = origin.rstrip('/')
    parts = urlsplit(origin)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment):
        raise Fault('Expected an HTTPS gateway origin')
    token = read_secret(key_file)
    site_access = None
    access_file = os.path.join(os.path.dirname(key_file), 'site-access.json')
    if os.path.exists(access_file):
        with open(access_file) as handle:
            record = json.load(handle)
        if record.get('origin') != origin:
            raise Fault('Private Site access belongs to a different origin', 403)
        site_access = record['token']
    return origin, token, site_access
