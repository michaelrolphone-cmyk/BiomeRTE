"""Site initialization, service context, enrollment and signed calls."""

# Native recovery checks this literal before executing a marked source home.
# Keep version 1 only while the source/site and event custody guards remain.
CUSTODY_PROTOCOL = 1

import contextlib
import hmac
import json
import os
import time
from .api import Context, OPERATIONS, command, op, ident
from .store import Fault, Store, Etcd, uid, encode, mkdir, digest, text
from .gateway_journal import GatewayJournal
from .identity import secret, write_private, generate, fingerprint, verify
from . import knowledge, work, process, providers, exchange, extensions, decisions, authorization, assistant_support, discovery
from . import streams
from . import retention, session_api
from .release import identity as release_identity
assistant_support.install_hooks()


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def initialize(directory, site=None):
    """Create private node material once. Each clone gets a new identity."""
    mkdir(directory)
    path = os.path.join(directory, 'node.json')
    if os.path.isfile(path):
        return read_json(path)
    key = os.path.join(directory, 'node.key')
    if os.path.exists(key):
        # Recover interrupted init using the same private identity.
        from .identity import openssl
        pub = openssl(['rsa', '-in', key, '-pubout']).decode('ascii')
    else:
        pub = generate(key)
    conf = {'format': 1, 'site': ident(site or uid()), 'node': fingerprint(pub), 'public_key': pub,
            'incarnation': uid(), 'admin_token': secret(), 'listen': '127.0.0.1', 'port': 8740}
    write_private(path, (json.dumps(conf, indent=2) + '\n').encode())
    return conf


def seed(tx):
    decisions.seed(tx, Context(None))
    # Backfill this built-in's guide for existing realms as well as fresh nodes.
    if tx.get('_contract_usage', 'core.echo:1') is None:
        from .capability_usage import ECHO_USAGE
        tx.put('_contract_usage', 'core.echo:1', ECHO_USAGE)
    if not tx.get('_meta', 'initialized'):
        ctx = Context(None)
        providers.define(tx, ctx, 'core.echo', {'value': 'any'}, {
                         'value': 'any'}, description='Return the input value.')
        providers.offer(tx, ctx, 'builtin', 'core.echo:1', 'builtin.echo')
        tx.put('_meta', 'initialized', 1)


class Application:
    def __init__(self, directory, controller_runtime=None):
        self.directory = os.path.abspath(directory)
        self.config = read_json(os.path.join(directory, 'node.json'))
        self.node, self.site = self.config['node'], self.config['site']
        self.key = os.path.join(directory, 'node.key')
        self.controller_runtime = dict(controller_runtime or {})
        self.provider_contacts = {}
        self.controller_aliases = frozenset()
        self.controller_operator_actors = frozenset()
        self.controller_actor_aliases = {}
        self._management_stamp, self._management_value = None, None
        if self.controller_runtime:
            c = self.controller_runtime
            if (c.get('authority_kind') != 'controller' or c.get('authority_id') != self.node
                    or c.get('site') != self.site or not isinstance(c.get('service_id'), str)):
                raise Fault('Managed controller runtime identity mismatch', 403)
            aliases = c.get('controller_aliases', [])
            operators = c.get('operator_actors', [])
            actor_aliases = c.get('operator_actor_aliases', {})
            if (not isinstance(aliases, list) or not all(isinstance(x, str) and x for x in aliases)
                    or not isinstance(operators, list) or not all(isinstance(x, str) and x for x in operators)
                    or not isinstance(actor_aliases, dict) or set(actor_aliases) - set(operators)
                    or any(v != 'admin' for v in actor_aliases.values())):
                raise Fault('Explicit controller aliases and operator identities required', 403)
            self.controller_aliases = frozenset(aliases)
            self.controller_operator_actors = frozenset(operators)
            self.controller_actor_aliases = dict(actor_aliases)
        remote = None
        if self.config.get('gateway_link') and (self.config.get('gateway_journal') or self.config.get('etcd')):
            raise Fault('Local gateway link cannot use permanent shared storage')
        realm_file = 'site'
        if self.config.get('etcd'):
            remote = Etcd(self.config['etcd'], self.site)
            realm_file = 'cluster-' + self.site
        if self.config.get('gateway_journal'):
            remote = GatewayJournal(self.config['gateway_journal'], self.site)
            realm_file = 'gateway-' + self.site
        self.stores = {'local': Store(directory, 'local'),
                       'site': Store(directory, realm_file, remote,
                           rollback_journal=self.controller_runtime.get("site_rollback_journal") is True)}
        self.stores['site'].write_guard = self._site_write_allowed
        self.timer_anchors = {}
        self.retention_next = {}
        self.health = {}
        for scope, store in self.stores.items():
            if scope == 'site' and self._controller_management() and not self.controller_runtime:
                # Frozen sources and passive logical homes remain inspectable.
                self.health[scope] = 'managed-read-only'
                continue
            try:
                store.execute(seed)
                self.health[scope] = 'ready'
            except Fault as e:
                self.health[scope] = str(e)
                if scope == 'local' or e.status != 503:
                    raise

    def _controller_management(self):
        path = os.path.join(self.directory, 'controller-authority.json')
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            if self._management_value is not None:
                raise Fault('Controller custody marker was removed', 409)
            return None
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if stamp != self._management_stamp:
            try:
                value = read_json(path)
            except (OSError, ValueError) as exc:
                raise Fault('Invalid controller custody marker', 409) from exc
            if (not isinstance(value, dict) or value.get('format') != 1
                    or value.get('mode') not in ('frozen-source', 'managed-authority')
                    or value.get('site') != self.site
                    or not all(isinstance(value.get(k), str) and value[k]
                               for k in ('service_id', 'authority_id'))
                    or value['mode'] == 'frozen-source' and value.get('source_node') != self.node
                    or value['mode'] == 'managed-authority' and value['authority_id'] != self.node):
                raise Fault('Controller custody marker identity mismatch', 409)
            self._management_stamp, self._management_value = stamp, value
        return self._management_value

    def _site_write_allowed(self):
        marker = self._controller_management()
        if marker is None:
            return
        runtime = self.controller_runtime
        if (marker['mode'] != 'managed-authority' or not runtime
                or any(marker[k] != runtime.get(k) for k in ('site', 'service_id', 'authority_id'))
                or set(marker.get('controller_aliases', [])) != self.controller_aliases):
            raise Fault('Site responsibility is managed by logical authority ' +
                        marker['authority_id'] + '; native site writes are frozen', 409)

    def call(self, name, args=None, request_id=None, realm='site', auth=None, cached=False, controller=None):
        # Verify the original addressed controller below; never rewrite signed input.
        accepted = self.controller_aliases if realm == 'site' else frozenset()
        if controller is not None and controller != self.node and controller not in accepted:
            raise Fault('Request addressed to a different controller', 403)
        if realm not in self.stores:
            raise Fault('Unknown realm')
        args = {} if args is None else args

        def authorize(tx):
            ctx = Context(self, scope=realm)
            if auth is not None:
                peer = tx.need('peers', auth['actor'])
                if not peer['enabled']:
                    raise Fault('Revoked credential', 403)
                envelope = {'site': self.site, 'realm': realm,
                            'operation': name, 'arguments': args, 'id': request_id}
                if controller is not None:
                    envelope['controller'] = controller
                verify(peer['key'], auth['signature'], envelope)
                operator_actor = realm == 'site' and peer['id'] in self.controller_operator_actors
                actor = self.controller_actor_aliases.get(peer['id'], peer['id']) if operator_actor else peer['id']
                ctx = Context(self, actor, realm, None if operator_actor else peer['scopes'])
                if (self.controller_runtime and realm == 'site' and not cached and not operator_actor
                        and name in ('cap.offer', 'cap.publish', 'provider.poll') and args.get('provider') == peer['id']):
                    ctx.authenticated_provider = peer['id']
                if operator_actor:
                    ctx.authenticated_actor = peer['id']
                if not operator_actor and (name.startswith('peer.') or name.startswith('pair.') or name == 'system.snapshot'):
                    raise Fault('Operator action', 403)
            return ctx

        def transaction(tx):
            ctx = authorize(tx)
            if operation is not None and operation[2] and self.config.get('operational_retention', {}).get('enabled', True):
                with retention.tracking(tx):
                    return command(tx, ctx, name, args, request_id)
            return command(tx, ctx, name, args, request_id)
        operation = OPERATIONS.get(name)
        passive = name in ('system.info', 'cluster.status', 'bus.discover', 'system.snapshot')
        if realm == 'site' and (operation is None or operation[2] or not passive):
            # provider.poll, stream.inspect and bus reads can expose commands
            # despite being read-only. A frozen source may serve diagnostics only.
            self._site_write_allowed()
        if name in session_api.EXTERNAL:
            if cached and session_api.EXTERNAL[name][1]:
                raise Fault('Cached site view is read-only', 409)
            return session_api.dispatch(self, name, args, request_id, realm, authorize)
        read_only = operation is not None and not operation[2]
        result = self.stores[realm].execute(transaction, cached=cached, read_only=read_only)
        if realm == 'site' and not passive:
            self._site_write_allowed()
        return result

    def tick(self, realm):
        if realm == 'site':
            self._site_write_allowed()
        budget = 64
        policy = self.config.get('operational_retention', {})
        enabled = policy.get('enabled', True)
        now = time.time()
        due = enabled and now >= self.retention_next.get(realm, 0)
        def work_tick(tx):
            seed(tx)
            ctx = Context(self, scope=realm)
            tx.ownership_execution = True
            try:
                moved = process.tick(tx, ctx, budget=budget)
                providers.settle_submissions(tx, ctx)
                assistant_support.pump(tx, ctx)
            finally:
                # Retention and canonical dependency reads must see every row,
                # including the temporarily fenced responsibility.
                tx.ownership_execution = False
            # Disposable local projection catches up every tick, including while
            # payload retention is disabled for a staged rollout. No journal
            # changes are emitted by advance itself.
            from .retention_index import advance
            index_ready = advance(tx.db, max_rows=policy.get('index_rows', 64),
                                  max_chars=policy.get('index_chars', 131072))
            if due and index_ready:
                retention.sweep(tx, now=now, seconds=policy.get('seconds', 86400),
                                keep=policy.get('keep', 1024), batch=policy.get('batch', 128), index_ready=True)
            return moved
        def run(tx):
            if enabled:
                with retention.tracking(tx, now):
                    return work_tick(tx)
            return work_tick(tx)
        while True:
            try:
                result = self.stores[realm].execute(run)
                if due:
                    self.retention_next[realm] = now + policy.get('interval', 60)
                return result
            except Fault as exc:
                # A rolled-back batch has dispatched no durable effects.
                # Reduce the batch, preserving each activity's atomic commit.
                if exc.status != 413 or budget == 1:
                    raise
                budget = max(1, budget // 2)

    def begin_pair(self, pub, label):
        key = fingerprint(pub)

        def run(tx):
            prior = tx.get('candidates', key)
            if prior and prior['state'] in ('pending', 'approved'):
                return {'challenge': prior['challenge'], 'site': self.site, 'key': key}
            if len([r for r in tx.rows('candidates') if r['state'] == 'pending']) >= 64:
                raise Fault('Commissioning inbox full', 429)
            record = {'id': key, 'key': pub, 'label': text(
                label), 'challenge': secret(), 'state': 'pending'}
            tx.put('candidates', key, record)
            return {'challenge': record['challenge'], 'site': self.site, 'key': key}
        return self.stores['site'].execute(run)

    def finish_pair(self, proof):
        def run(tx):
            p = tx.need('candidates', proof['key'])
            if p['state'] not in ('approved', 'enrolled'):
                raise Fault('Verify device fingerprint on the commissioning link first', 403)
            msg = {'challenge': p['challenge'], 'site': self.site, 'key': p['id']}
            verify(p['key'], proof['signature'], msg)
            if p['state'] == 'approved':
                exchange.trust(tx, Context(self), p['key'], p['label'], p['scopes'])
                p['state'] = 'enrolled'
                tx.put('candidates', p['id'], p)
            if not tx.need('peers', p['id'])['enabled']:
                raise Fault('Revoked', 403)
            return {'site': self.site, 'credential': p['id'], 'scopes': p['scopes']}
        return self.stores['site'].execute(run)


@op('pair.list', write=False)
def candidates(tx, ctx):
    return [{k: r[k] for k in ('id', 'label', 'state')} for r in tx.rows('candidates')]


@op('pair.approve', 'fingerprint:str scopes:list')
def approve(tx, ctx, fingerprint, scopes):
    r = tx.need('candidates', fingerprint)
    if r['state'] != 'pending':
        raise Fault('Candidate no longer pending', 409)
    if not all(isinstance(s, str) for s in scopes):
        raise Fault('Invalid scope')
    r.update(state='approved', scopes=scopes)
    tx.put('candidates', fingerprint, r)
    return {'id': fingerprint, 'state': 'approved'}


@op('system.info', write=False)
def info(tx, ctx):
    return {'node': ctx.app.node, 'site': ctx.app.site, 'realm': ctx.scope, 'version': '0.1.0',
            'release': release_identity(),
            'shared_backend': 'etcd' if ctx.app.stores['site'].remote else 'sqlite',
            'position': tx.db.execute('SELECT seq FROM position WHERE id=1').fetchone()[0]}


@op('cluster.status', write=False)
def cluster_status(tx, ctx):
    managed = getattr(ctx.app, 'controller_runtime', {})
    if ctx.scope == 'site' and managed:
        return {'mode':'managed-controller', 'node':ctx.app.node,
                'automatic_failover':True, 'service_id':managed['service_id'],
                'authority_id':managed['authority_id'],
                'controller_aliases':sorted(ctx.app.controller_aliases),
                'replica_workers':managed.get('replica_workers',[]),
                'replication_policy':managed.get('replication_policy','available-peers'),
                'placement_binding':getattr(ctx.app,'controller_binding',None),
                'freshness_evidence':'Current grant and exact durable head are checked by the service helper and coordinator.'}
    marker = ctx.app._controller_management() if ctx.scope == 'site' else None
    if marker:
        return {'mode':'managed-source', 'node':ctx.app.node,
                'automatic_failover':False, 'site_writes':'frozen',
                'service_id':marker['service_id'],'authority_id':marker['authority_id']}
    remote = ctx.app.stores['site'].remote
    if not remote:
        return {'mode': 'standalone', 'node': ctx.app.node, 'automatic_failover': False}
    if isinstance(remote, GatewayJournal):
        return dict(remote.status(), mode='gateway-replicated', node=ctx.app.node,
                    site_position=tx.db.execute('SELECT seq FROM position WHERE id=1').fetchone()[0])
    members = remote.gateway('cluster/member/list', {}, remote.urls).get('members', [])
    observations = []
    for url in remote.urls:
        try:
            state = remote.gateway('maintenance/status', {}, [url])
            observations.append({'url': url, 'member': state['header'].get('member_id'), 'leader': state.get(
                'leader'), 'applied': state.get('raftAppliedIndex', state.get('raft_applied_index'))})
        except Fault:
            observations.append({'url': url, 'state': 'unreachable'})
    return {'mode': 'replicated', 'members': members, 'observations': observations, 'site_position': tx.db.execute('SELECT seq FROM position WHERE id=1').fetchone()[0]}


@op('system.health', write=False)
def system_health(tx, ctx):
    """Read the controller, consumer progress and cluster in one transaction."""
    for component in ('system.info', 'system.progress', 'cluster.status'):
        if not ctx.allowed(component):
            raise Fault('Permission denied', 403)
    return {'node': ctx.app.node,
            'health': {'realms': {ctx.scope: 'ready'}, 'consumer_health': discovery.progress(tx, ctx)},
            'controller': info(tx, ctx), 'cluster': cluster_status(tx, ctx)}


@op('system.snapshot', write=False)
def snapshot(tx, ctx):
    return {'site': info(tx, ctx), 'work': work.listing(tx, ctx), 'process': process.listing(tx, ctx),
            'waits': process.waits(tx, ctx), 'capabilities': providers.discover(tx, ctx),
            'leases': tx.rows('leases'), 'documents': tx.rows('documents'),
            'mission': tx.get('mission', 'current', {}), 'inbox': tx.rows('messages')[-50:],
            'subscriptions': tx.rows('subscriptions'), 'candidates': candidates(tx, ctx)}


@op('demo.run', 'message?:str')
def demo(tx, ctx, message='Hello from BiomeRTE'):
    """Create a full goal-to-task example, pin a routine, start it and return real IDs."""
    g = work.create(tx, ctx, 'goal', 'Demonstrate a portable work cycle')
    m = work.create(tx, ctx, 'milestone', 'Collect a provider result', parent=g['id'])
    w = work.create(tx, ctx, 'workflow', 'Run and retain', parent=m['id'])
    t = work.create(tx, ctx, 'task', 'Echo a message and save it',
                    parent=w['id'], criteria=['stored result'])
    steps = [{'type': 'service', 'contract': 'core.echo:1', 'args': {'value': {'$': 'input.message'}}, 'save': 'reply'},
             {'type': 'service', 'operation': 'knowledge.put', 'args': {'key': {'$': 'input.key'},
                                                                        'body': {'$': 'reply.value'}, 'links': [t['id']]}, 'save': 'doc'}]
    process.routine(tx, ctx, 'demo.echo-and-save', steps, {'message': 'str', 'key': 'str'}, {'revision': 'str'},
                    {'revision': {'$': 'doc.rev'}})
    r = process.start(tx, ctx, 'demo.echo-and-save',
                      {'message': message, 'key': 'demo-' + t['id']}, t['id'])
    return {'goal': g['id'], 'task': t['id'], 'run': r['id'], 'document': 'demo-' + t['id']}
