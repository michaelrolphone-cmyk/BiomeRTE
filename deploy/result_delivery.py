"""Bounded completion frames; preserve the entire MCP result (Python 3.4+)."""
import hashlib
import json

# A framing size, not a result limit. ASCII JSON also bounds UTF-8 byte size.
CHUNK_SIZE = 65536


def complete(body, send, persistent=None, delivery_socket=None):
    encoded = json.dumps(body['result'], ensure_ascii=True, separators=(',', ':'))
    if len(encoded) <= CHUNK_SIZE:
        if persistent is not None:
            acknowledged = persistent.complete(body, delivery_socket)
            if acknowledged is not None:
                return acknowledged
        return send('complete', body)
    digest = hashlib.sha256(encoded.encode('ascii')).hexdigest()
    parts = (len(encoded) + CHUNK_SIZE - 1) // CHUNK_SIZE
    identity = {'id': body['id'], 'lease': body['lease'], 'digest': digest}
    for part in range(parts):
        chunk = dict(identity, part=part, data=encoded[part * CHUNK_SIZE:(part + 1) * CHUNK_SIZE])
        response = send('result-chunk', chunk)
        if not response.get('accepted'):
            return response
    return send('result-commit', dict(identity, parts=parts))
