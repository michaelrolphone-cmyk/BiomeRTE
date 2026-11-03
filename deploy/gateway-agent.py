#!/usr/bin/env python3
"""Outbound connector for a BiomeRTE controller. Compatible with Python 3.4+."""
import argparse
import hashlib
import json
import os
import socket
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = build_opener(NoRedirect)


def fetch(url, payload=None, token=None, timeout=18):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {'User-Agent': 'BiomeRTE-Controller-Link/1.0', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url, data=data, headers=headers)
    with opener.open(req, timeout=timeout) as res:
        body = res.read(470001)
    if len(body) > 470000:
        raise ValueError('Controller response exceeds the gateway size limit. Use a narrower operation.')
    return json.loads(body.decode('utf-8'))


def dispatch(job, local, conf):
    call = job['request']
    name, args = call['name'], call.get('arguments', {})
    if name == 'biome_status':
        status = fetch(local + '/health')
        return {'content': [{'type': 'text', 'text': json.dumps({'host': socket.gethostname(), 'node': conf['node'], 'health': status})}]}
    if name == 'biome_work_list':
        target, inputs = 'invoke', {'operation': 'work.list', 'arguments': {}}
    elif name == 'biome_discover':
        target, inputs = 'discover', {}
    elif name == 'biome_invoke':
        target, inputs = 'invoke', args
        if not inputs.get('request_id'):
            raise ValueError('request_id required')
    else:
        raise ValueError('Unknown controller tool')
    rpc = {'jsonrpc': '2.0', 'id': job['id'], 'method': 'tools/call', 'params': {'name': target, 'arguments': inputs}}
    value = fetch(local + '/mcp', rpc, conf['admin_token'], timeout=30)
    if 'result' not in value:
        raise ValueError('Controller rejected the MCP request')
    return value['result']


def read_secret(path):
    with open(path) as f:
        secret = f.read().strip()
    if len(secret) != 64 or any(c not in '0123456789abcdef' for c in secret):
        raise ValueError('Invalid dedicated bridge key')
    return secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--port', type=int, default=8740)
    parser.add_argument('--home', default=os.environ.get('BIOME_HOME', os.path.expanduser('~/.local/share/biomerte')))
    parser.add_argument('--key', default=os.path.expanduser('~/.local/share/biomerte-gateway/bridge.key'))
    parser.add_argument('--pin', action='store_true', help='Print the public verifier for the gateway environment')
    args = parser.parse_args()
    origin = args.origin.rstrip('/')
    parts = urlsplit(origin)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise SystemExit('Expected an HTTPS gateway origin')
    token = read_secret(args.key)
    if args.pin:
        first = hashlib.sha256(token.encode('ascii')).hexdigest()
        print(hashlib.sha256((first + '\n').encode('ascii')).hexdigest())
        return
    local = 'http://127.0.0.1:' + str(args.port)
    pause = 2
    while True:
        try:
            job = fetch(origin + '/bridge/next', {}, token)
            pause = 2
            if job.get('idle'):
                time.sleep(4)
                continue
            try:
                with open(os.path.join(args.home, 'node.json')) as f:
                    result = dispatch(job, local, json.load(f))
            except Exception as exc:
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'Controller request failed: ' + str(exc)}]}
            try:
                fetch(origin + '/bridge/complete', {'id': job['id'], 'lease': job['lease'], 'result': result}, token)
            except HTTPError as exc:
                if exc.code != 409:
                    raise
        except Exception as exc:
            print('Gateway link: ' + str(exc), file=sys.stderr, flush=True)
            time.sleep(pause)
            pause = min(45, pause * 2)


if __name__ == '__main__':
    main()
