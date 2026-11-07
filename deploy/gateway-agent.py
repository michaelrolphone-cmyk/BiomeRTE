#!/usr/bin/env python3
"""Outbound connector for a BiomeRTE controller. Compatible with Python 3.4+."""
import argparse
import hashlib
import json
import os
import socket
import ssl
import sys
import time
import threading
from mcp_events import EventStore, CallbackError
from result_delivery import complete
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = build_opener(NoRedirect, HTTPSHandler(context=ssl.create_default_context()))


def fetch(url, payload=None, token=None, timeout=18, site_access=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {'User-Agent': 'BiomeRTE-Controller-Link/1.0', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if site_access:
        headers['OAI-Sites-Authorization'] = 'Bearer ' + site_access
    req = Request(url, data=data, headers=headers)
    with opener.open(req, timeout=timeout) as res:
        body = res.read()
    return json.loads(body.decode('utf-8'))


def dispatch(job, local, conf):
    call = job['request']
    name, args = call['name'], call.get('arguments', {})
    if name == 'biome_native_provider':
        from native_provider import dispatch_native_provider
        return dispatch_native_provider(args, local, conf)
    if name == 'biome_status':
        status = fetch(local + '/health')
        rpc = {'jsonrpc': '2.0', 'id': str(job['id']) + '-progress', 'method': 'tools/call',
               'params': {'name': 'invoke', 'arguments': {'operation': 'system.progress', 'arguments': {}}}}
        try:
            progress = fetch(local + '/mcp', rpc, conf['admin_token'])['result']
            status['consumer_health'] = (json.loads(progress['content'][0]['text']) if not progress.get('isError')
                                         else {'state': 'unavailable', 'reason': progress['content'][0]['text']})
        except Exception as error:
            status['consumer_health'] = {'state': 'unavailable', 'reason': type(error).__name__}
        return {'content': [{'type': 'text', 'text': json.dumps({'host': socket.gethostname(), 'node': conf['node'], 'health': status})}]}
    if name == 'biome_work_list':
        target, inputs = 'invoke', {'operation': 'work.list', 'arguments': {}}
    elif name == 'biome_discover':
        target, inputs = 'invoke', {'operation': 'discovery.page', 'arguments': args}
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


def event_service(path, local, home, origin, token, site_access):
    def read_feed(realm, operation, arguments):
        with open(os.path.join(home, 'node.json')) as f:
            credential = json.load(f)['admin_token']
        call = {'operation': operation, 'arguments': arguments, 'realm': realm}
        rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'invoke', 'arguments': call}}
        result = fetch(local + '/mcp', rpc, credential)['result']
        if result.get('isError'):
            raise ValueError('Controller delegation feed unavailable')
        return json.loads(result['content'][0]['text'])

    def authorize(owner, grant):
        return fetch(origin + '/bridge/event-authorize', {'owner': owner, 'grant': grant}, token,
                     site_access=site_access)['allowed']
    def feed(realm, after, limit):
        return read_feed(realm, 'delegation.feed', {'after': after, 'limit': limit})

    def topics(realm, topic, after, limit):
        return read_feed(realm, 'bus.event_page', {'topic': topic, 'after': after, 'limit': limit})

    return EventStore(path, feed, authorize, topic_feed=topics)


def delivery_loop(factory):
    store = factory()
    while True:
        try:
            if store.active():
                store.deliver()
        except Exception as exc:
            # Callback URLs and signing secrets must never reach logs.
            print('Event delivery retry: ' + type(exc).__name__, file=sys.stderr, flush=True)
        time.sleep(2)


def event_change(store, args):
    try:
        result = store.change(args['method'], args['params'], args['owner'], args['grant'])
        body = {'result': result}
    except CallbackError as exc:
        body = {'error': {'code': -32015, 'message': 'Callback verification failed', 'data': {'reason': str(exc)}}}
    except (ValueError, TypeError, KeyError):
        body = {'error': {'code': -32602, 'message': 'Invalid subscription parameters'}}
    return {'content': [{'type': 'text', 'text': json.dumps(body)}]}


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
    site_access = None
    access_file = os.path.join(os.path.dirname(args.key), 'site-access.json')
    if os.path.exists(access_file):
        with open(access_file) as f:
            access = json.load(f)
        if access.get('origin') != origin:
            raise SystemExit('Private Site access belongs to a different origin')
        site_access = access['token']
    with open(os.path.join(args.home, 'node.json')) as handle:
        conf = json.load(handle)
    if conf.get('gateway_journal'):
        from ha_link import run
        return run(args, origin, token, site_access, fetch, dispatch, event_service, event_change)
    events_file = os.path.join(os.path.dirname(args.key), 'events.sqlite')
    factory = lambda: event_service(events_file, local, args.home, origin, token, site_access)
    events = factory()
    thread = threading.Thread(target=delivery_loop, args=(factory,))
    thread.daemon = True
    thread.start()
    pause = 2
    while True:
        try:
            job = fetch(origin + '/bridge/next', {}, token, site_access=site_access)
            pause = 2
            if job.get('idle'):
                time.sleep(4)
                continue
            try:
                if job['request']['name'] == 'grok_receiver_change':
                    from grok_receivers import mutate
                    call = job['request']['arguments']
                    try:
                        value = mutate(events, call['action'], call['args'], call['owner'], call['grant'])
                        result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
                    except (ValueError, TypeError, KeyError):
                        # Library exceptions can include the supplied URL or key.
                        result = {'isError': True, 'content': [{'type':'text','text':'Invalid receiver settings, connection ownership or request ID.'}]}
                elif job['request']['name'] == 'mcp_event_change':
                    result = event_change(events, job['request']['arguments'])
                else:
                    with open(os.path.join(args.home, 'node.json')) as f:
                        result = dispatch(job, local, json.load(f))
            except Exception as exc:
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'Controller request failed: ' + str(exc)}]}
            try:
                complete({'id': job['id'], 'lease': job['lease'], 'result': result},
                    lambda path, body: fetch(origin + '/bridge/' + path, body, token, site_access=site_access))
            except HTTPError as exc:
                if exc.code != 409:
                    raise
        except Exception as exc:
            print('Gateway link: ' + str(exc), file=sys.stderr, flush=True)
            time.sleep(pause)
            pause = min(45, pause * 2)


if __name__ == '__main__':
    main()
