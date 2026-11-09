"""Forward native provider requests without acquiring controller authority.

Python 3.4 compatible. The device's exact original envelope is sent to the
ordinary /v1/call handler with NO operator bearer header. That handler verifies
the RSA signature, enabled peer, operation scope and resource ownership before
reading a cached mutation receipt.
"""
import base64
import binascii
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

OPERATIONS = frozenset(('cap.offer', 'provider.poll', 'cap.fenced',
                       'provider.claim', 'provider.result', 'cap.status',
                       'cap.configured', 'agent.progress'))
IDENT = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
ACTOR = re.compile(r'^[0-9a-f]{64}$')


def answer(status, body):
    return {'content': [{'type': 'text', 'text': json.dumps(
        {'status': status, 'body': body}, separators=(',', ':'))}]}


def valid_envelope(data, site):
    if not isinstance(data, dict) or set(data) != set(
            ('site', 'realm', 'id', 'operation', 'arguments', 'auth')):
        return False
    if data['site'] != site or data['realm'] != 'site':
        return False
    if (not isinstance(data['id'], str) or not IDENT.match(data['id']) or
            '\n' in data['id'] or data['operation'] not in OPERATIONS or
            not isinstance(data['arguments'], dict)):
        return False
    auth = data['auth']
    if not isinstance(auth, dict) or set(auth) != set(('actor', 'signature')):
        return False
    actor, signature = auth['actor'], auth['signature']
    if not isinstance(actor, str) or not ACTOR.match(actor) or '\n' in actor:
        return False
    if not isinstance(signature, str) or not signature:
        return False
    try:
        if not base64.b64decode(signature.encode('ascii'), validate=True):
            return False
    except (ValueError, UnicodeError, binascii.Error):
        return False
    return data['operation'] == 'cap.status' or data['arguments'].get('provider') == actor


def dispatch_signed_provider(arguments, local, site, opener):
    """Return a native HTTP reply inside the durable gateway's result envelope."""
    if not isinstance(arguments, dict) or set(arguments) != set(('envelope',)):
        return answer(400, {'error': 'Invalid signed-provider transport'})
    raw = arguments['envelope']
    if not isinstance(raw, str):
        return answer(400, {'error': 'Expected original JSON envelope'})
    try:
        data = json.loads(raw)
        valid = valid_envelope(data, site)
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        return answer(400, {'error': 'Invalid scoped-provider envelope'})
    return forward_native(raw, local, '/v1/call', opener)


def forward_native(raw, local, path, opener):
    address = urlsplit(local)
    if address.scheme != 'http' or address.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return answer(500, {'error': 'Provider ingress requires controller loopback'})
    request = Request(local.rstrip('/') + path, data=raw.encode('utf-8'),
                      headers={'Content-Type': 'application/json'})
    # Deliberately no Authorization header and no admin credential argument.
    # This timeout only concerns the HTTP exchange, never the physical action.
    try:
        with opener.open(request, timeout=30) as response:
            status, payload = response.getcode(), response.read()
    except HTTPError as error:
        status, payload = error.code, error.read()
    try:
        body = json.loads(payload.decode('utf-8'))
        if not isinstance(body, dict):
            raise ValueError('Expected object')
    except (ValueError, UnicodeError):
        return answer(502, {'error': 'Invalid native controller response', 'retry_same_id': True})
    return answer(status, body)


def dispatch_provider_pair(arguments, local, site, opener):
    """Expose only the native candidate/proof steps; approval stays owner-only."""
    if not isinstance(arguments, dict) or set(arguments) != set(('phase', 'envelope')):
        return answer(400, {'error': 'Invalid pairing transport'})
    phase, raw = arguments['phase'], arguments['envelope']
    if phase not in ('begin', 'finish') or not isinstance(raw, str):
        return answer(400, {'error': 'Invalid pairing phase'})
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Expected object')
        if phase == 'begin':
            valid = (set(data) == set(('public_key', 'label')) and
                     isinstance(data['public_key'], str) and
                     len(data['public_key']) <= 8192 and
                     'BEGIN PUBLIC KEY' in data['public_key'] and
                     isinstance(data['label'], str) and bool(data['label']))
        else:
            valid = (set(data) == set(('challenge', 'site', 'key', 'signature')) and
                     data['site'] == site and
                     isinstance(data['challenge'], str) and
                     ACTOR.match(data['challenge']) and '\n' not in data['challenge'] and
                     isinstance(data['key'], str) and ACTOR.match(data['key']) and
                     '\n' not in data['key'] and isinstance(data['signature'], str) and
                     bool(base64.b64decode(data['signature'].encode('ascii'), validate=True)))
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        valid = False
    if not valid:
        return answer(400, {'error': 'Invalid pairing envelope'})
    return forward_native(raw, local, '/v1/pair/' + phase, opener)
