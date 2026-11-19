"""Private router transport and authenticated remote lease-authority reads."""
import hashlib
import hmac
import json
import time
import urllib.request
from urllib.parse import urlsplit


def loopback_url(value):
    if value is None:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != 'ws' or parsed.hostname not in ('127.0.0.1', '::1', 'localhost')
        or parsed.username or parsed.password or parsed.path not in ('', '/')
        or parsed.query or parsed.fragment):
        raise ValueError('Native router must be a loopback WebSocket listener')
    return value.rstrip('/')


def inspect_remote(authority, settings, session):
    body = json.dumps({'authority': authority, 'session': session,
                       'timestamp': int(time.time())}, separators=(',', ':')).encode()
    signature = hmac.new(settings['secret'].encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(settings['inspect_url'], data=body, headers={
        'Content-Type': 'application/json', 'Authorization': 'Router ' + signature,
        'User-Agent': 'BiomeRTE-Private-Router/1'})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def add_remote(config, secrets, validators):
    for authority, settings in config.get('remote_controllers', {}).items():
        parsed = urlsplit(settings['inspect_url'])
        if (len(authority) != 64 or any(c not in '0123456789abcdef' for c in authority)
            or authority in secrets or len(settings['secret']) < 32
            or parsed.scheme != 'https' or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('Invalid remote stream authority')
        secrets[authority] = settings['secret']
        validators[authority] = lambda session, node=authority, cfg=dict(settings): inspect_remote(node, cfg, session)
