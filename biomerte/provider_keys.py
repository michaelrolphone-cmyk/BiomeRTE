"""Scoped machine keys for providers whose registration IDs already exist."""
import re

from .api import Context, ident, op
from .identity import fingerprint, verify
from .store import Fault


PROVIDER_OPERATIONS = frozenset((
    'provider.auth_status', 'provider.poll', 'provider.claim', 'provider.result',
    'provider.custody', 'provider.reconcile', 'cap.offer', 'cap.fenced',
    'cap.configured', 'cap.heartbeat', 'cap.command.state', 'cap.status',
    'cap.registrations', 'cap.publish', 'cap.withdraw', 'cap.define',
    'cap.discover', 'artifact.put', 'stream.attach',
))


@op('peer.bind_provider_key', 'public_key:str provider:str resources:list contracts:list')
def bind(tx, ctx, public_key, provider, resources, contracts):
    """Bind a verified machine key to an existing provider and its offer scope."""
    if ctx.scope != 'site' or not re.fullmatch('[a-f0-9]{64}', provider):
        raise Fault('Expected an existing site provider identity')
    key = fingerprint(public_key)
    if tx.get('peers', key):
        raise Fault('Machine key already has peer authority', 409)
    if not resources or not contracts or not all(isinstance(v, str) for v in resources + contracts):
        raise Fault('Expected explicit provider resource and contract scopes')
    resources, contracts = sorted(set(map(ident, resources))), sorted(set(map(ident, contracts)))
    offers = [offer for offer in tx.rows('offers') if offer['provider'] == provider]
    if set(resources) != {o['resource'] for o in offers} or set(contracts) != {o['contract'] for o in offers}:
        raise Fault('Machine key scope does not match the existing provider registrations', 403)
    value = dict(key=public_key, provider=provider, resources=resources,
                 contracts=contracts, offers=sorted({o['resource'] + '/' + o['contract'] for o in offers}),
                 enabled=True)
    prior = tx.get('_provider_machine_keys', key)
    if prior and prior != value:
        raise Fault('Machine key was bound or revoked with different authority', 409)
    tx.put('_provider_machine_keys', key, value)
    return dict(actor=key, provider=provider, resources=resources, contracts=contracts, enabled=True)


@op('peer.revoke_provider_key', 'actor:str')
def revoke(tx, ctx, actor):
    """Revoke a machine key without changing the provider registration."""
    value = tx.need('_provider_machine_keys', actor)
    tx.put('_provider_machine_keys', actor, dict(value, enabled=False))
    return dict(actor=actor, provider=value['provider'], enabled=False)


@op('provider.auth_status', write=False)
def status(tx, ctx):
    """Probe signed provider authentication without polling or claiming work."""
    if not getattr(ctx, 'machine_key', None):
        raise Fault('Machine provider authentication required', 403)
    return dict(provider=ctx.actor, actor=ctx.machine_key, authentication='machine_key')


def authorize(app, realm, key, signature, envelope, binding):
    if realm != 'site' or not binding['enabled']:
        raise Fault('Machine provider credential revoked or outside its site', 403)
    verify(binding['key'], signature, envelope)
    name, args, provider = envelope['operation'], envelope['arguments'], binding['provider']
    contracts, resources = binding['contracts'], binding['resources']
    if name not in PROVIDER_OPERATIONS or args.get('provider', provider) != provider:
        raise Fault('Operation outside machine provider grant', 403)
    if name == 'cap.define':
        contract = '{}:{}'.format(args.get('name'), args.get('version', 1))
        if contract not in contracts:
            raise Fault('Contract outside machine provider grant', 403)
    if name == 'cap.discover' and args.get('contract') not in contracts:
        raise Fault('Contract outside machine provider grant', 403)
    if name == 'cap.offer' and '{}/{}'.format(args.get('resource'), args.get('contract')) not in binding['offers']:
        raise Fault('Offer outside machine provider grant', 403)
    if name == 'cap.publish' and isinstance(args.get('offers'), list):
        for offer in args['offers']:
            if isinstance(offer, dict) and '{}/{}'.format(args.get('resource'), offer.get('contract')) not in binding['offers']:
                raise Fault('Offer outside machine provider grant', 403)
    if name == 'stream.attach' and args.get('role') != 'provider':
        raise Fault('Machine provider key requires the provider stream role', 403)
    permissions = (PROVIDER_OPERATIONS | {'offer:' + c for c in contracts}
                   | {'resource:' + r for r in resources})
    ctx = Context(app, provider, realm, permissions)
    ctx.machine_key = key
    if name in ('cap.offer', 'cap.publish', 'provider.poll') and args.get('provider') == provider:
        ctx.authenticated_provider = provider
    return ctx
