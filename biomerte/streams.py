"""Controller-issued stream grants; payload transport belongs to the live router."""
import base64
import hashlib
import hmac
import json
import time
from .api import op, ident
from .store import Fault, uid, encode
from . import lifecycle


def settings(ctx):
    secret = ctx.app.config.get('stream_router_secret', '')
    url = ctx.app.config.get('stream_gateway_url', '')
    if len(secret) < 32 or not url.startswith('wss://'):
        raise Fault('Configure stream_router_secret and a wss stream_gateway_url for the persistent router', 503)
    return secret, url


def authorize(tx, ctx, grant, role='server'):
    expected = grant['server'] if role == 'server' else grant['provider']
    if ctx.permissions is not None and ctx.actor != expected:
        raise Fault('Stream attachment identity mismatch', 403)


def valid(tx, ctx, grant):
    lease = tx.need('leases', grant['lease'])
    guard = tx.need('guards', lease['guard'])
    if (grant['state'] != 'open' or grant['expires_at'] <= time.time() or
            grant['authority'] != ctx.app.node or grant['realm'] != ctx.scope or
            lease['state'] != 'bound' or
            (guard['holder'], guard['generation']) != (lease['id'], grant['generation'])):
        raise Fault('Stream authority superseded', 409)
    peer = tx.get('peers', grant['server'])
    provider = tx.get('peers', grant['provider'])
    if (peer and not peer['enabled']) or (provider and not provider['enabled']):
        raise Fault('Stream identity revoked', 403)
    return grant


def token(ctx, grant, role):
    secret, unused = settings(ctx)
    payload = dict(grant, role=role, credential_expires_at=min(grant['expires_at'], time.time() + 120))
    raw = encode(payload).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + signature


@op('stream.open', 'lease:str ttl_seconds?:int')
def open_stream(tx, ctx, lease, ttl_seconds=3600):
    secret, url = settings(ctx)
    if not 0 < ttl_seconds <= 86400:
        raise Fault('Invalid stream lifetime')
    l = tx.need('leases', lease)
    if ctx.permissions is not None and l.get('owner') != ctx.actor:
        raise Fault('Lease belongs to another task owner', 403)
    if l['state'] != 'bound' or lifecycle.policy(tx, l)['lease_class'] != 'persistent':
        raise Fault('Streaming requires a bound persistent lease', 409)
    offer = tx.need('offers', l['offer'])
    stream = offer.get('configuration', {}).get('stream', {})
    if stream.get('enabled') is not True:
        raise Fault('Offer does not declare a streaming interface', 409)
    if ctx.permissions is not None and not tx.need('peers', ctx.actor)['enabled']:
        raise Fault('Paired server required', 403)
    from .authorization import guard
    guard(tx, 'stream-' + lease, l['contract'], l['resource'], {}, ctx)
    for prior in tx.rows('_stream_grants'):
        if prior['lease'] == lease and prior['state'] == 'open':
            raise Fault('Stream already open; attach or close the existing session', 409)
    direction = stream.get('direction', 'provider_to_server')
    delivery = stream.get('delivery', 'supersedable')
    if direction not in ('provider_to_server', 'server_to_provider', 'bidirectional'):
        raise Fault('Invalid stream direction')
    if delivery not in ('supersedable', 'ordered', 'ack_required'):
        raise Fault('Invalid delivery policy')
    limits = {}
    for key, default, maximum in (('max_message_bytes', 1048576, 4194304),
                                  ('buffer_depth', 1, 64), ('messages_per_second', 60, 1000)):
        n = stream.get(key, default)
        if type(n) is not int or not 1 <= n <= maximum:
            raise Fault('Invalid stream limit ' + key)
        limits[key] = n
    lanes = stream.get('lanes', ['data'])
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= 8 or any(lane not in ('data', 'vision', 'telemetry', 'pose', 'map', 'control') for lane in lanes):
        raise Fault('Invalid stream lanes')
    policies = stream.get('policies', {})
    if not isinstance(policies, dict) or any(k not in lanes or v not in ('supersedable', 'ordered', 'ack_required') for k,v in policies.items()):
        raise Fault('Invalid per-lane delivery policies')
    grant = dict(id=uid(), epoch=uid(), lease=lease, authority=ctx.app.node, site=ctx.app.site,
        realm=ctx.scope, contract=l['contract'], resource=l['resource'], provider=l['provider'],
        generation=l['generation'], server=ctx.actor, direction=direction, delivery=delivery,
        limits=limits, policies=policies, lanes=stream.get('lanes', ['data']), schema=stream.get('schema', {}), state='open', expires_at=time.time() + ttl_seconds)
    tx.put('_stream_grants', grant['id'], grant)
    return dict(grant, url=url.rstrip('/') + '/' + grant['id'], role='server', attachment_credential=token(ctx, grant, 'server'))


@op('stream.attach', 'session:str role:str', False)
def attach(tx, ctx, session, role):
    if role not in ('server', 'provider'):
        raise Fault('Invalid stream role')
    grant = valid(tx, ctx, tx.need('_stream_grants', session))
    authorize(tx, ctx, grant, role)
    unused, url = settings(ctx)
    return dict(grant, url=url.rstrip('/') + '/' + session, role=role, attachment_credential=token(ctx, grant, role))


@op('stream.inspect', 'session:str', False)
def inspect(tx, ctx, session):
    grant = tx.need('_stream_grants', session)
    if ctx.permissions is not None and ctx.actor not in (grant['provider'], grant['server']):
        raise Fault('Other stream', 403)
    return valid(tx, ctx, grant)


@op('stream.close', 'session:str')
def close(tx, ctx, session):
    grant = tx.need('_stream_grants', session)
    authorize(tx, ctx, grant)
    grant.update(state='closed', expires_at=time.time())
    return tx.put('_stream_grants', session, grant)


@op('stream.revoke', 'session:str')
def revoke(tx, ctx, session):
    if ctx.permissions is not None:
        raise Fault('Operator access required', 403)
    grant = tx.need('_stream_grants', session)
    grant.update(state='revoked', expires_at=time.time())
    return tx.put('_stream_grants', session, grant)

@op('stream.list', 'lease?:str', False)
def listing(tx, ctx, lease=''):
    return [g for g in tx.rows('_stream_grants') if (not lease or g['lease'] == lease) and
            (ctx.permissions is None or ctx.actor in (g['server'], g['provider'])) and
            g['state'] == 'open' and g['expires_at'] > time.time()]
