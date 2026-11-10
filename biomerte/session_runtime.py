"""Local SQLite execution with an explicitly bounded temporary shared session.

Network calls never hold SQLite transactions. Only selected durable records enter
this temporary log. Current pending writes and undelivered signed packages remain
local until reconciled, never in a permanent shared ledger.
"""
import json
import os
import fcntl
import time
import uuid
import threading
from functools import wraps
from .store import Store, Fault, encode, digest
from .session_exchange import validate_scope, identity, export_package, import_package, validate_records, outcome_identity
from .identity import signed, verify


MAX_PACKET_BYTES = 64 * 1024 * 1024
MAX_OUTBOX_BYTES = 64 * 1024 * 1024

DEFAULT_POLICY = {'max_entries': 512, 'seconds': 3600, 'max_bytes': 16 * 1024 * 1024}


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            if self._depth:
                return method(self, *args, **kwargs)
            # CLI and background maintenance can be separate processes. Hold a
            # local advisory lock, never a SQLite transaction, across I/O.
            with open(self._lock_path, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    return method(self, *args, **kwargs)
                finally:
                    self._depth = 0
                    fcntl.flock(lock, fcntl.LOCK_UN)
    return call


class SessionRuntime:
    def __init__(self, app, directory, remote, business_store):
        if business_store.remote is not None:
            raise Fault('Temporary sessions require a local SQLite business store', 409)
        self._lock = threading.RLock()
        self.app, self.remote, self.business = app, remote, business_store
        self.store = Store(directory, 'coordination')
        self._lock_path = os.path.join(directory, 'session.lock')
        self._depth = 0

    def state(self):
        return self.store.execute(lambda tx: tx.get('_coordination', 'active'), read_only=True)

    def _set(self, state):
        self.store.execute(lambda tx: tx.put('_coordination', 'active', state))

    @serialized
    def open(self, scope, participants, baselines, peer_keys, policy=None, authorized_closers=None):
        validate_scope(scope)
        if self.state() is not None:
            raise Fault('A local coordination session is still unresolved', 409)
        policy = dict(DEFAULT_POLICY if policy is None else policy)
        policy.setdefault('packet_bytes', MAX_PACKET_BYTES)
        policy.setdefault('outbox_bytes', MAX_OUTBOX_BYTES)
        if (type(policy['packet_bytes']) is not int or not 0 < policy['packet_bytes'] <= MAX_PACKET_BYTES or
                type(policy['outbox_bytes']) is not int or not 0 < policy['outbox_bytes'] <= MAX_OUTBOX_BYTES):
            raise Fault('Invalid finite packet/outbox limits')
        if any(type(policy.get(k)) is not int or policy[k] <= 0 for k in DEFAULT_POLICY):
            raise Fault('Session policy requires finite positive bounds')
        if (not isinstance(participants, list) or len(set(participants)) != len(participants) or
                self.app.node not in participants or not 2 <= len(participants) <= 32):
            raise Fault('At least two distinct session participants are required')
        if policy['max_entries'] > 100000 or policy['max_bytes'] > 1073741824:
            raise Fault('Session bounds exceed supported transport limits')
        authorized_closers = [self.app.node] if authorized_closers is None else authorized_closers
        if (not isinstance(authorized_closers, list) or self.app.node not in authorized_closers or
                len(set(authorized_closers)) != len(authorized_closers) or
                not set(authorized_closers).issubset(participants)):
            raise Fault('Explicit finite authorized closers must be session participants')
        authorized_closers = sorted(authorized_closers)
        keys = validate_scope(scope)
        if any(set(baselines.get(node, {})) != keys for node in participants):
            raise Fault('Every participant requires an exact baseline')
        if set(peer_keys) != set(participants) or peer_keys.get(self.app.node) != self.app.config['public_key']:
            raise Fault('Exact participant public keys required', 403)
        def authorize(tx):
            for participant in participants:
                if participant == self.app.node:
                    continue
                peer = tx.need('peers', participant)
                if (not peer.get('enabled') or 'coordination' not in peer.get('scopes', []) or
                        peer.get('key') != peer_keys[participant]):
                    raise Fault('Participant lacks explicit coordination authority', 403)
        self.business.execute(authorize, read_only=True)
        rows = self.business.execute(lambda tx: [[kind, key, tx.get(kind, key)] for kind, key in scope], read_only=True)
        self.business.execute(lambda tx: validate_records(tx, [], scope, self.app, peer_keys), read_only=True)
        for kind, key, value in rows:
            if digest(value) != baselines[self.app.node][identity(kind, key)]:
                raise Fault('Local baseline changed before session open', 409)
        if len(encode(rows).encode('utf-8')) > policy['max_bytes']:
            raise Fault('Baseline exceeds selected session byte budget', 413)
        state = {'id': uuid.uuid4().hex, 'state': 'reserving', 'participants': participants,
                 'scope': scope, 'baselines': baselines, 'peer_keys': peer_keys,
                 'expires': time.time() + policy['seconds'], 'policy': policy,
                 'seq': 0, 'hash': '', 'bytes': 0, 'owner': self.app.node, 'initial': rows}
        state['authorized_closers'] = authorized_closers
        state['successor_after'] = state['expires']
        state['authorization_digest'] = digest(self._authorization(state))
        self.business.execute(lambda tx:self._check_package_budget(tx,state),read_only=True)
        self._set(state)
        self.business.execute(lambda tx: self._reserve_scope(tx, state))
        state['state'] = 'opening'
        def initialize(tx):
            if tx.need('_coordination', 'active')['id'] != state['id']:
                raise Fault('Concurrent local session open', 409)
            for kind, key, value in rows:
                if value is not None:
                    tx.put(kind, key, value)
            validate_records(tx, [], scope, self.app, peer_keys)
            tx.put('_coordination', 'active', state)
        self.store.execute(initialize)
        # An uncertain reply leaves the opening intent local. No blind retry.
        try:
            self.remote('open', {'session': state['id'], 'participants': participants,
                        'scope': scope, 'expires': state['expires'],
                        'max_entries': policy['max_entries'], 'max_bytes': policy['max_bytes'],
                        'authorized_closers': authorized_closers, 'successor_after': state['successor_after']})
        except Fault as error:
            if error.status in (400, 409, 413):
                self.recover_open()
            raise
        state['state'] = 'open'
        self._set(state)
        return state

    @serialized
    def recover_open(self):
        state = self.state()
        if not state or state['state'] != 'opening':
            raise Fault('No uncertain session opening', 409)
        try:
            remote = self.remote('status', {'session': state['id']})
        except Fault as error:
            if error.status != 410:
                raise
            # The owner has never attested an outbox; an absent opening was
            # not admitted. Drop only the local intent, never replay open.
            def abort(tx):
                for kind, key in state['scope']:
                    tx.drop(kind, key)
                tx.drop('_coordination', 'active')
            self.business.execute(lambda tx:self._release_scope(tx,state['id']))
            self.store.execute(abort)
            return {'active': False, 'opened': False}
        definition = json.loads(remote['definition'])
        if (remote.get('owner') != self.app.node or remote.get('session') != state['id'] or
                set(definition['participants']) != set(state['participants']) or
                validate_scope(definition['scope']) != validate_scope(state['scope']) or
                remote['expires'] != state['expires'] or
                remote['max_entries'] != state['policy']['max_entries'] or
                remote['max_bytes'] != state['policy']['max_bytes'] or
                definition.get('authorized_closers', [state['owner']]) != state['authorized_closers'] or
                definition.get('successor_after', state['expires']) != state['successor_after']):
            raise Fault('Opened session differs from local intent', 503)
        state['state'] = 'open'
        self._set(state)
        return self.synchronize()

    @serialized
    def synchronize(self):
        state = self.state()
        if not state:
            raise Fault('No local session', 404)
        for unused in range(state['policy']['max_entries'] + 1):
            page = self.remote('read', {'session': state['id'], 'after': state['seq']})
            record = page.get('record')
            if record is None:
                if page['final_seq'] != state['seq'] or page['final_hash'] != state['hash']:
                    raise Fault('Session final head differs from local state', 503)
                if page.get('state') == 'frozen':
                    self.store.execute(lambda tx: tx.drop('_coordination', 'pending'))
                    state.update(state='closing', closer=page.get('closer') or state['owner'],
                                 closer_epoch=page.get('closer_epoch'),closer_until=page.get('closer_until',0),
                                 final_head={'seq': state['seq'], 'hash': state['hash']})
                    self._set(state)
                return state
            raw = record['entry']
            entry = json.loads(raw)
            import hashlib
            hashed = hashlib.sha256(raw.encode('utf-8')).hexdigest()
            if (record['hash'] != hashed or entry['seq'] != state['seq'] + 1 or
                    record['seq'] != entry['seq'] or entry['prev'] != state['hash']):
                raise Fault('Temporary session continuity mismatch', 503)
            allowed = validate_scope(state['scope'])
            changes, seen = [], set()
            for kind, key, value in entry['changes']:
                ident = identity(kind, key)
                if ident not in allowed or ident in seen or (value is not None and not isinstance(value, str)):
                    raise Fault('Session contains invalid durable changes', 503)
                seen.add(ident)
                decoded = json.loads(value) if value is not None else None
                if value is not None and encode(decoded) != value:
                    raise Fault('Session value is not canonical JSON', 503)
                changes.append((kind, key, decoded))
            previous = dict(state)
            state = dict(state, seq=entry['seq'], hash=hashed,
                         bytes=state['bytes'] + len(raw.encode('utf-8')))
            def apply(tx):
                current = tx.need('_coordination', 'active')
                if (current['seq'], current['hash']) != (previous['seq'], previous['hash']):
                    raise Fault('Local session advanced', 409)
                validate_records(tx, changes, state['scope'], self.app, state['peer_keys'])
                for kind, key, value in changes:
                    tx.drop(kind, key) if value is None else tx.put(kind, key, value)
                pending = tx.get('_coordination', 'pending')
                if pending and json.loads(pending['entry'])['seq'] == entry['seq']:
                    # This immutable position proves acceptance of our exact
                    # proposal, or rejection in favor of the competing proposal.
                    tx.drop('_coordination', 'pending')
                tx.put('_coordination', 'active', state)
            self.store.execute(apply)
        raise Fault('Session exceeded its entry bound', 503)

    @serialized
    def append(self, changes):
        state = self.synchronize()
        if state['state'] != 'open' or time.time() >= state['expires']:
            self.maintain()
            raise Fault('Session admission closed; finalize it', 410)
        allowed, rows, seen = validate_scope(state['scope']), [], set()
        for kind, key, value in changes:
            ident = identity(kind, key)
            if ident not in allowed or ident in seen:
                raise Fault('Change lies outside exact durable session scope')
            seen.add(ident)
            rows.append([kind, key, None if value is None else encode(value)])
        if not rows:
            raise Fault('Empty session transaction')
        self.store.execute(lambda tx: validate_records(tx, changes, state['scope'], self.app, state['peer_keys']), read_only=True)
        self.store.execute(lambda tx:self._check_package_budget(tx,state,changes),read_only=True)
        entry = encode({'seq': state['seq'] + 1, 'prev': state['hash'], 'changes': rows})
        size = len(entry.encode('utf-8'))
        if size > 1048576:
            raise Fault('Single session transaction exceeds transport admission', 413)
        if state['seq'] >= state['policy']['max_entries'] or state['bytes'] + size > state['policy']['max_bytes']:
            state['needs_close'] = True
            self._set(state)
            self.maintain()
            raise Fault('Session bound reached; finalization requested', 413)
        import hashlib
        pending = {'entry': entry, 'hash': hashlib.sha256(entry.encode('utf-8')).hexdigest()}
        def reserve(tx):
            if tx.get('_coordination', 'pending'):
                raise Fault('A session write still requires reconciliation', 409)
            current = tx.need('_coordination', 'active')
            if (current['seq'], current['hash']) != (state['seq'], state['hash']):
                raise Fault('Local session advanced', 409)
            tx.put('_coordination', 'pending', pending)
        self.store.execute(reserve)
        try:
            self.remote('append', {'session': state['id'], 'expected': state['seq'], 'entry': entry})
        except Fault as error:
            if error.status in (400, 409, 410, 413):
                # The server explicitly rejected this CAS/admission. No accepted
                # outcome is hidden by these responses; do not retain a false debt.
                self.store.execute(lambda tx: tx.drop('_coordination', 'pending'))
            raise
        # Reading the accepted entry reconciles a lost append reply without
        # resubmitting the operation. No completed local write ledger is kept.
        return self.synchronize()

    @serialized
    def finalize(self):
        prior = self.state()
        if not prior:
            raise Fault('No local session', 404)
        if self.app.node not in prior.get('authorized_closers', [prior['owner']]):
            raise Fault('Not an explicitly authorized session closer', 403)
        if self.app.node != prior['owner'] and time.time() < prior['successor_after']:
            raise Fault('Successor closing authority is not yet eligible', 409)
        if prior.get('closer') and prior['closer'] != self.app.node and time.time() < prior.get('closer_until',0):
            return {'active':True, 'awaiting_closer':prior['closer'], 'session':prior['id']}
        if prior and prior.get('state') == 'delivery_pending':
            self._complete_local_delivery()
            return {'closed': True, 'pending_delivery': self.pending_recipients()}
        if prior.get('state') == 'close_uncertain':
            return {'active':False,'close_uncertain':True,'session':prior['id']}
        if prior.get('outbox_ready'):
            try:
                remote = self.remote('status', {'session': prior['id']})
            except Fault as error:
                if error.status != 410:
                    raise
                if (len(prior.get('authorized_closers',[])) > 1 and time.time() >= prior.get('closer_until',0)):
                    prior['state'] = 'close_uncertain'
                    self._set(prior)
                    return {'active':False,'close_uncertain':True,'session':prior['id']}
            else:
                if remote.get('closer') and remote['closer'] != self.app.node:
                    return self._yield_closer(prior,remote)
                if (remote.get('state') != 'frozen' or (remote.get('closer') or remote.get('owner')) != self.app.node or
                        remote.get('seq') != prior['seq'] or remote.get('hash') != prior['hash']):
                    raise Fault('Gateway differs from durable outbox intent', 503)
                packages = []
                for node in prior['participants']:
                    packet = self.store.execute(lambda tx:tx.need('_outbox',node),read_only=True)
                    packages.append({'to':node,'package_digest':digest(packet['payload'])})
                result = self._attest_outbox(prior,packages)
                if result.get('awaiting_closer'):
                    return result
                if not result.get('closed'):
                    raise Fault('Gateway did not confirm session deletion', 503)
            prior['state'] = 'delivery_pending'
            self._set(prior)
            self._complete_local_delivery()
            return {'closed': True, 'pending_delivery': self.pending_recipients()}
        state = self.synchronize()
        if state.get('closer') and state['closer'] != self.app.node and time.time() < state.get('closer_until',0):
            return {'active':True, 'awaiting_closer':state['closer'], 'session':state['id']}
        try:
            frozen = self.remote('freeze', {'session': state['id'],
                                 'final_seq': state['seq'], 'final_hash': state['hash']})
        except Fault as error:
            if error.status != 409:
                raise
            status = self.remote('status', {'session':state['id']})
            if status.get('state') != 'frozen' or not status.get('closer'):
                raise
            state = self.synchronize()
            if status['closer'] != self.app.node:
                state['closer'] = status['closer']
                self._set(state)
                return {'active':True, 'awaiting_closer':status['closer'], 'session':state['id']}
            frozen = {'final_seq':status['seq'], 'final_hash':status['hash'], 'closer':status['closer']}
        closer = frozen.get('closer') or state['owner']
        if closer != self.app.node:
            raise Fault('Gateway selected a different session closer', 409)
        # Freeze serializes against any uncertain in-flight append. Its final
        # head proves whether that position was accepted or rejected.
        self.store.execute(lambda tx: tx.drop('_coordination', 'pending'))
        state.update(state='closing', closer=closer, closer_epoch=frozen.get('closer_epoch',1),
                     closer_until=frozen.get('closer_until',time.time()+60), final_head={'seq': frozen['final_seq'], 'hash': frozen['final_hash']})
        self._set(state)
        packages, total = [], 0
        for node in sorted(state['participants']):
            packet = self.store.execute(lambda tx:export_package(tx,self.app,state,node,state['final_head']),read_only=True)
            size = len(encode(packet).encode('utf-8'))
            total += size
            if size > state['policy']['packet_bytes'] or total > state['policy']['outbox_bytes']:
                raise Fault('Final signed package outbox exceeds admitted bounds',413)
            self.store.execute(lambda tx:tx.put('_outbox',node,packet))
            packages.append({'to':node,'package_digest':digest(packet['payload'])})
            state['outcome_digest'] = packet['payload']['data']['outcome_digest']
        # Persist recovery intent before the attestation that atomically deletes
        # the gateway session. A lost reply is reconciled from this local outbox.
        state['outbox_ready'] = True
        self._set(state)
        result = self._attest_outbox(state,packages)
        if result.get('awaiting_closer'):
            return result
        if not result.get('closed'):
            raise Fault('Gateway did not confirm session deletion', 503)
        state['state'] = 'delivery_pending'
        self._set(state)
        # Gateway history is gone; only actual unresolved local delivery remains.
        self._complete_local_delivery()
        return result


    def _yield_closer(self,state,status):
        def discard(tx):
            for node in state['participants']:
                tx.drop('_outbox',node)
            state.pop('outbox_ready',None)
            state.update(state='closing',closer=status['closer'],closer_epoch=status.get('closer_epoch'),
                         closer_until=status.get('closer_until',0))
            tx.put('_coordination','active',state)
        self.store.execute(discard)
        return {'active':True,'awaiting_closer':state['closer'],'session':state['id']}

    def _attest_outbox(self,state,packages):
        try:
            claim = self.remote('freeze',{'session':state['id'],'final_seq':state['seq'],'final_hash':state['hash']})
            if (claim.get('closer') or state['owner']) != self.app.node:
                return self._yield_closer(state,claim)
            prior_epoch = state.get('closer_epoch')
            state.update(closer=self.app.node,closer_epoch=claim.get('closer_epoch',1),
                         closer_until=claim.get('closer_until',time.time()+60))
            self._set(state)
            if state['closer_epoch'] != prior_epoch:
                packages = []
                for node in state['participants']:
                    packet = self.store.execute(lambda tx:export_package(tx,self.app,state,node,state['final_head']),read_only=True)
                    self.store.execute(lambda tx:tx.put('_outbox',node,packet))
                    packages.append({'to':node,'package_digest':digest(packet['payload'])})
            return self.remote('outbox',{'session':state['id'],'final_seq':state['seq'],
                'final_hash':state['hash'],'closer_epoch':state['closer_epoch'],'durable':True,'packages':packages})
        except Fault as error:
            # An authorization denial ends this remote action immediately.
            # Only explicit claim contention permits a status reconciliation.
            if error.status != 409:
                raise
            status = self.remote('status',{'session':state['id']})
            if status.get('closer') and status['closer'] != self.app.node:
                return self._yield_closer(state,status)
            raise

    def _check_package_budget(self,tx,state,changes=()):
        values = {(kind,key):value for kind,key,value in changes}
        total = 0
        for node in state['participants']:
            records = []
            for kind,key in sorted(state['scope']):
                value = values.get((kind,key),tx.get(kind,key))
                records.append([kind,key,None if value is None else encode(value),state['baselines'][node][identity(kind,key)]])
            payload = {'format':1,'site':self.app.site,'sender':self.app.node,'to':node,'kind':'session-state',
                'data':{'session':state['id'],'final_seq':state['policy']['max_entries'],'final_hash':'f'*64,
                        'closer':max(state['participants'],key=len),'closer_epoch':9999999999999999,
                        'outcome_digest':'f'*64,'authorization_digest':state['authorization_digest'],
                        'scope':state['scope'],'records':records}}
            # Conservative admission reserves a bounded full signature envelope.
            # Final export additionally measures the actual signed packets.
            size = len(encode({'payload':payload,'signature':'x'*16384}).encode('utf-8'))
            if size > state['policy']['packet_bytes']:
                raise Fault('Resulting signed package exceeds transport bound',413)
            total += size
        if total > state['policy']['outbox_bytes']:
            raise Fault('Resulting recipient outbox exceeds total local budget',413)
        invitation = {'format':1,'site':self.app.site,'sender':state['owner'],'to':max(state['participants'],key=len),
                      'kind':'session-invitation','data':dict(self._authorization(state),authorization_digest=state['authorization_digest'])}
        if len(encode({'payload':invitation,'signature':'x'*16384}).encode('utf-8')) > state['policy']['packet_bytes']:
            raise Fault('Signed invitation exceeds transport bound',413)

    def pending_recipients(self):
        with self.store.connect() as db:
            return [row[0] for row in db.execute('SELECT id FROM records WHERE kind=? ORDER BY id', ('_outbox',))]

    def _verify_ack(self, state, node, packet, acknowledgement):
        envelope = acknowledgement.get('payload', {})
        if (envelope.get('format') != 1 or envelope.get('kind') != 'session-ack' or
                envelope.get('site') != self.app.site or envelope.get('sender') != node or
                envelope.get('to') != self.app.node):
            raise Fault('Invalid recipient acknowledgement',403)
        verify(state['peer_keys'][node],acknowledgement.get('signature'),envelope)
        proof = envelope.get('data',{})
        if (proof.get('session') != state['id'] or proof.get('final_head') != state['final_head'] or
                proof.get('package_digest') != digest(packet['payload'])):
            raise Fault('Recipient import acknowledgement mismatch',503)
        return signed(self.app.key,{'format':1,'site':self.app.site,'sender':self.app.node,
            'to':node,'kind':'session-acceptance','data':dict(proof,ack=acknowledgement)})

    def _deliver_one(self,state,node,deliver):
        packet = self.store.execute(lambda tx:tx.need('_outbox',node),read_only=True)
        if packet['payload']['kind'] == 'session-state':
            ack = (self._import_and_ack(dict(state,state='closing'),packet) if node == self.app.node
                   else deliver(node,packet))
            acceptance = self._verify_ack(state,node,packet,ack)
            # Replace large state with the exact signed receipt and small pending
            # settlement before asking the receiver to erase unresolved evidence.
            self.store.execute(lambda tx:tx.put('_outbox',node,acceptance))
            packet = acceptance
        confirmation = self.settle(packet) if node == self.app.node else deliver(node,packet)
        body = confirmation.get('payload',{})
        expected = {'session':state['id'],'package_digest':packet['payload']['data']['package_digest'],
                    'acceptance_digest':digest(packet['payload'])}
        if (body.get('format') != 1 or body.get('kind') != 'session-settled' or
                body.get('site') != self.app.site or body.get('sender') != node or
                body.get('to') != self.app.node or body.get('data') != expected):
            raise Fault('Invalid delivery settlement confirmation',403)
        verify(state['peer_keys'][node],confirmation.get('signature'),body)
        self.store.execute(lambda tx:tx.drop('_outbox',node))

    def _complete_local_delivery(self):
        state = self.state()
        if not state or state['state'] != 'delivery_pending' or self.app.node not in self.pending_recipients():
            return
        try:
            self._deliver_one(state,self.app.node,None)
        except Fault as error:
            if error.status != 409:
                raise
            state['local_import_conflict'] = str(error)
            self._set(state)

    @serialized
    def deliver_pending(self,deliver):
        state = self.state()
        if not state or state['state'] != 'delivery_pending':
            raise Fault('Session has not exported and closed',409)
        for node in self.pending_recipients():
            self._deliver_one(state,node,deliver)
        def discard(tx):
            if tx.rows('_outbox') or tx.get('_coordination','pending'):
                raise Fault('Local coordination still has unresolved work',409)
            for kind,key in state['scope']:
                tx.drop(kind,key)
            tx.drop('_coordination','active')
        self.store.execute(discard)
        return {'closed':True,'pending_delivery':[]}

    @serialized
    def inspect_reconciliation(self):
        state = self.state()
        outbox = []
        for node in self.pending_recipients():
            packet = self.store.execute(lambda tx:tx.need('_outbox',node),read_only=True)
            body = packet['payload']; data = body['data']
            outbox.append({'to':node,'kind':body['kind'],'payload_digest':digest(body),
                'signature':packet['signature'],'outcome_digest':data.get('outcome_digest'),
                'final_head':data.get('final_head',{'seq':data.get('final_seq'),'hash':data.get('final_hash')}),
                'closer_epoch':data.get('closer_epoch')})
        facts = {'session':state['id'] if state else None,'state':state['state'] if state else None,
            'head':state.get('final_head') if state else None,'closer':state.get('closer') if state else None,
            'closer_epoch':state.get('closer_epoch') if state else None,
            'closer_until':state.get('closer_until') if state else None,
            'outcome_digest':state.get('outcome_digest') if state else None,'pending_outbox':outbox}
        return signed(self.app.key,{'format':1,'site':self.app.site,'sender':self.app.node,
            'kind':'session-reconciliation-facts','data':facts})

    @serialized
    def settle(self,packet):
        payload = packet.get('payload',{})
        data = payload.get('data',{})
        ack = data.get('ack',{})
        ack_body = ack.get('payload',{})
        if (payload.get('format') != 1 or payload.get('kind') != 'session-acceptance' or
                payload.get('site') != self.app.site or payload.get('to') != self.app.node or
                ack_body.get('format') != 1 or ack_body.get('kind') != 'session-ack' or
                ack_body.get('site') != self.app.site or ack_body.get('sender') != self.app.node or
                ack_body.get('to') != payload.get('sender') or
                ack_body.get('data') != {k:v for k,v in data.items() if k != 'ack'}):
            raise Fault('Invalid signed delivery acceptance',403)
        peer = ({'key':self.app.config['public_key'],'enabled':True,'scopes':['coordination']}
                if payload['sender'] == self.app.node else self.business.execute(
                    lambda tx:tx.need('peers',payload['sender']),read_only=True))
        if not peer.get('enabled') or 'coordination' not in peer.get('scopes',[]):
            raise Fault('Settlement sender lacks coordination authority',403)
        verify(peer['key'],packet.get('signature'),payload)
        verify(self.app.config['public_key'],ack.get('signature'),ack_body)
        def clear(tx):
            prior = tx.get('_session_delivery_ack',data['package_digest'])
            if prior:
                if prior['ack'] != ack or prior['owner_key'] != peer['key']:
                    raise Fault('Settlement does not match unresolved receipt',409)
                tx.drop('_session_delivery_ack',data['package_digest'])
        self.business.execute(clear)
        return signed(self.app.key,{'format':1,'site':self.app.site,'sender':self.app.node,
            'to':payload['sender'],'kind':'session-settled','data':{'session':data['session'],
            'package_digest':data['package_digest'],'acceptance_digest':digest(payload)}})

    @serialized
    def maintain(self):
        state = self.state()
        if not state:
            return {'active': False}
        if state['state'] == 'reserving':
            self.business.execute(lambda tx:self._release_scope(tx,state['id']))
            def abort(tx):
                for kind,key in state['scope']:
                    tx.drop(kind,key)
                tx.drop('_coordination','active')
            self.store.execute(abort)
            return {'active':False,'opened':False}
        if state['state'] == 'close_uncertain':
            return {'active':False,'close_uncertain':True,'session':state['id']}
        if state['state'] == 'opening':
            return self.recover_open()
        if (state.get('closer') and state['closer'] != self.app.node and
                (time.time() < state.get('closer_until',0) or self.app.node not in state.get('authorized_closers',[]))):
            return {'active':True, 'session':state['id'], 'awaiting_closer':state['closer']}
        if (self.app.node != state['owner'] and
                (self.app.node not in state.get('authorized_closers', []) or time.time() < state['successor_after'])):
            return {'active': True, 'session': state['id'], 'awaiting_owner': True}
        if state['state'] == 'delivery_pending':
            return {'active': False, 'pending_delivery': self.pending_recipients()}
        # A participant may have advanced the bounded shared head since our
        # last local write. Observe it before deciding whether closure is due.
        if state['state'] == 'open':
            state = self.synchronize()
        if (state.get('needs_close') or state['state'] == 'closing' or time.time() >= state['expires'] or
                state['seq'] >= state['policy']['max_entries'] or
                state['bytes'] >= state['policy']['max_bytes']):
            return self.finalize()
        return {'active': True, 'session': state['id']}

    @staticmethod
    def _authorization(state):
        return {key: state[key] for key in ('id', 'owner', 'participants', 'scope',
            'baselines', 'peer_keys', 'expires', 'policy', 'initial',
            'authorized_closers', 'successor_after')}

    @serialized
    def invitation(self, recipient):
        state = self.state()
        if (not state or state['state'] != 'open' or state['owner'] != self.app.node or
                recipient not in state['participants'] or recipient == self.app.node or
                time.time() >= state['expires']):
            raise Fault('No live owner session invitation for recipient', 409)
        data = dict(self._authorization(state), authorization_digest=state['authorization_digest'])
        return signed(self.app.key, {'format': 1, 'site': self.app.site, 'sender': self.app.node,
                      'to': recipient, 'kind': 'session-invitation', 'data': data})

    @serialized
    def join(self, packet):
        """Accept one explicitly supplied, finite signed owner invitation."""
        payload = packet.get('payload', {})
        data = payload.get('data', {})
        if (payload.get('format') != 1 or payload.get('site') != self.app.site or
                payload.get('to') != self.app.node or payload.get('kind') != 'session-invitation' or
                payload.get('sender') != data.get('owner') or data.get('owner') == self.app.node or
                self.app.node not in data.get('participants', [])):
            raise Fault('Invalid session invitation', 403)
        peer = self.business.execute(lambda tx: tx.need('peers', payload['sender']), read_only=True)
        if (not peer.get('enabled') or 'coordination' not in peer.get('scopes', []) or
                data.get('peer_keys', {}).get(payload['sender']) != peer['key']):
            raise Fault('Session owner is not trusted', 403)
        verify(peer['key'], packet.get('signature'), payload)
        existing = self.state()
        if existing is not None:
            if not (all(existing.get(key) == value for key, value in data.items()) and
                    existing.get('id') == data.get('id')):
                raise Fault('A different local coordination session is still unresolved', 409)
            if existing.get('state') != 'reserving':
                return existing
            # Intent/reservation is not a completed join. Revalidate the exact
            # signed invitation and resume idempotent reservation plus atomic
            # mirror initialization below before returning a joined result.
        allowed = validate_scope(data.get('scope'))
        policy = data.get('policy', {})
        participants = data.get('participants', [])
        if (any(type(policy.get(k)) is not int or policy[k] <= 0 for k in DEFAULT_POLICY) or
                data.get('expires', 0) <= time.time() or
                len(set(participants)) != len(participants) or not 2 <= len(participants) <= 32 or
                policy['max_entries'] > 100000 or policy['max_bytes'] > 1073741824 or
                type(policy.get('packet_bytes')) is not int or not 0 < policy['packet_bytes'] <= MAX_PACKET_BYTES or
                type(policy.get('outbox_bytes')) is not int or not 0 < policy['outbox_bytes'] <= MAX_OUTBOX_BYTES or
                set(data.get('peer_keys', {})) != set(participants) or
                data['peer_keys'].get(self.app.node) != self.app.config['public_key'] or
                any(set(data.get('baselines', {}).get(n, {})) != allowed for n in participants)):
            raise Fault('Invalid finite session authorization', 403)
        closers = data.get('authorized_closers')
        if (not isinstance(closers, list) or data['owner'] not in closers or
                len(set(closers)) != len(closers) or not set(closers).issubset(participants) or
                data.get('successor_after') != data['expires'] or
                data.get('authorization_digest') != digest(self._authorization(data))):
            raise Fault('Invalid finite closer authorization', 403)
        def authorize_closers(tx):
            for closer in closers:
                if closer == self.app.node:
                    continue
                peer = tx.need('peers',closer)
                if (not peer.get('enabled') or 'coordination' not in peer.get('scopes',[]) or
                        peer.get('key') != data['peer_keys'][closer]):
                    raise Fault('Authorized closer lacks existing local coordination authority',403)
        self.business.execute(authorize_closers,read_only=True)
        initial = data.get('initial', [])
        if (len(initial) != len(allowed) or
                {identity(r[0], r[1]) for r in initial} != allowed or
                len(encode(initial).encode('utf-8')) > policy['max_bytes']):
            raise Fault('Invalid session initial state')
        for kind, key, value in initial:
            if digest(value) != data['baselines'][data['owner']][identity(kind, key)]:
                raise Fault('Initial state differs from owner baseline', 409)
        self.business.execute(lambda tx: self._check_baseline(tx, data), read_only=True)
        self.business.execute(lambda tx: validate_records(tx, initial, data['scope'], self.app, data['peer_keys']), read_only=True)
        remote = self.remote('status', {'session': data['id']})
        definition = json.loads(remote['definition'])
        if (remote.get('owner') != data['owner'] or remote.get('session') != data['id'] or
                set(definition['participants']) != set(participants) or
                validate_scope(definition['scope']) != allowed or remote['expires'] != data['expires'] or
                remote['max_entries'] != policy['max_entries'] or remote['max_bytes'] != policy['max_bytes'] or
                definition.get('authorized_closers', [data['owner']]) != sorted(closers) or
                definition.get('successor_after', data['expires']) != data['successor_after']):
            raise Fault('Invitation differs from gateway session', 409)
        state = dict(data, state='reserving', seq=0, hash='', bytes=0)
        self._set(state)
        self.business.execute(lambda tx:self._reserve_scope(tx,state))
        state['state'] = 'open'
        def initialize(tx):
            if tx.need('_coordination', 'active')['id'] != state['id']:
                raise Fault('Concurrent local session join', 409)
            for kind, key, value in initial:
                if value is not None:
                    tx.put(kind, key, value)
            validate_records(tx, [], state['scope'], self.app, state['peer_keys'])
            tx.put('_coordination', 'active', state)
        self.store.execute(initialize)
        return self.synchronize()

    def _reserve_scope(self, tx, state):
        self._check_baseline(tx, state)
        for kind,key in state['scope']:
            ident = digest([kind,key])
            old = tx.get('_session_scope',ident)
            if old and old['session'] != state['id']:
                raise Fault('Durable record already reserved',409)
            tx.put('_session_scope',ident,{'id':ident,'session':state['id'],'kind':kind,'key':key})

    @staticmethod
    def _release_scope(tx, session_id):
        for row in tx.rows('_session_scope'):
            if row['session'] == session_id:
                tx.drop('_session_scope',row['id'])

    def _check_baseline(self, tx, state):
        for kind, key in state['scope']:
            if digest(tx.get(kind, key)) != state['baselines'][self.app.node][identity(kind, key)]:
                raise Fault('Local durable baseline changed', 409)

    @serialized
    def receive(self, packet):
        """Import an owner's final package and return a signed durable ACK.

        Import and bounded unresolved ACK commit atomically in the business database.
        A lost reply remains recoverable across later ordinary edits and partitions.
        Explicit signed settlement deletes the receipt without a tombstone.
        """
        payload = packet.get('payload', {})
        cache_id = digest(payload)
        cache = self.business.execute(lambda tx:tx.get('_session_delivery_ack',cache_id),read_only=True)
        if cache:
            sender = payload.get('sender')
            peer = ({'key':self.app.config['public_key'],'enabled':True,'scopes':['coordination']}
                    if sender == self.app.node else self.business.execute(lambda tx:tx.need('peers',sender),read_only=True))
            if (not peer.get('enabled') or 'coordination' not in peer.get('scopes',[]) or peer['key'] != cache['owner_key']):
                raise Fault('Cached receipt sender is revoked or changed',403)
            verify(cache['owner_key'], packet.get('signature'), payload)
            self._discard_received(cache['session'])
            return cache['ack']
        state = self.state()
        equivalent = self._equivalent_receipt(packet)
        if equivalent:
            if state and state['id'] == payload.get('data',{}).get('session'):
                self._discard_received(state['id'], superseded=True)
            return equivalent
        if not state:
            raise Fault('No accepted participant session for this package', 409)
        data = payload.get('data', {})
        epoch = data.get('closer_epoch')
        if (type(epoch) is not int or epoch < (state.get('closer_epoch') or 0) or
                (state.get('outbox_ready') and epoch <= (state.get('closer_epoch') or 0)) or
                (state.get('outcome_digest') and state['outcome_digest'] != data.get('outcome_digest'))):
            raise Fault('Final outcome or closing epoch conflicts with local proof',409)
        closer = payload.get('sender')
        if (closer not in state.get('authorized_closers', [state['owner']]) or
                (closer != state['owner'] and time.time() < state['successor_after']) or
                (state.get('closer') and state['closer'] != closer and epoch <= (state.get('closer_epoch') or 0)) or data.get('closer') != closer or
                data.get('authorization_digest') != state['authorization_digest'] or
                data.get('session') != state['id'] or
                type(data.get('final_seq')) is not int or data['final_seq'] < state['seq'] or
                data['final_seq'] > state['policy']['max_entries'] or
                not isinstance(data.get('final_hash'), str) or
                (data['final_seq'] == 0 and data['final_hash'] != '') or
                (data['final_seq'] > 0 and (len(data['final_hash']) != 64 or
                 any(c not in '0123456789abcdef' for c in data['final_hash'])))):
            raise Fault('Invalid final owner package', 409)
        if data['final_seq'] == state['seq'] and data['final_hash'] != state['hash']:
            raise Fault('Final package conflicts with observed session head', 409)
        closing = dict(state, state='closing', closer=closer, closer_epoch=epoch, final_head={'seq': data['final_seq'], 'hash': data['final_hash']})
        acknowledgement = self._import_and_ack(closing, packet)
        self._discard_received(state['id'],superseded=True)
        return acknowledgement

    def _equivalent_receipt(self,packet):
        payload = packet.get('payload',{})
        data = payload.get('data',{})
        if payload.get('kind') != 'session-state':
            return None
        candidates = self.business.execute(lambda tx:tx.rows('_session_delivery_ack'),read_only=True)
        matches = [row for row in candidates if row.get('session') == data.get('session') and
                   row.get('outcome_digest') == data.get('outcome_digest')]
        prior = max(matches,key=lambda row:row['ack']['payload']['data']['closer_epoch']) if matches else None
        if not prior:
            return None
        authority = prior['authorization']; sender = payload.get('sender')
        epoch = data.get('closer_epoch')
        if (payload.get('format') != 1 or payload.get('site') != self.app.site or payload.get('to') != self.app.node or
                sender not in authority['authorized_closers'] or data.get('closer') != sender or
                data.get('authorization_digest') != authority['authorization_digest'] or
                type(epoch) is not int or epoch < prior['ack']['payload']['data']['closer_epoch'] or
                (epoch == prior['ack']['payload']['data']['closer_epoch'] and sender != prior['ack']['payload']['to']) or
                (sender != authority['owner'] and time.time() < authority['successor_after']) or
                data.get('outcome_digest') != outcome_identity(data) or
                {'seq':data.get('final_seq'),'hash':data.get('final_hash')} != prior['final_head']):
            raise Fault('Equivalent receipt does not match unresolved frozen outcome',409)
        if (validate_scope(data.get('scope')) != set(prior['baseline']) or
                any(row[3] != prior['baseline'].get(identity(row[0],row[1])) for row in data['records'])):
            raise Fault('Equivalent outcome scope or baseline differs',409)
        peer = self.business.execute(lambda tx:tx.need('peers',sender),read_only=True)
        if (not peer.get('enabled') or 'coordination' not in peer.get('scopes',[]) or
                peer.get('key') != authority['peer_keys'][sender]):
            raise Fault('Equivalent outcome signer lacks coordination authority',403)
        verify(peer['key'],packet.get('signature'),payload)
        proof = dict(prior['ack']['payload']['data'],package_digest=digest(payload),closer_epoch=epoch)
        ack = signed(self.app.key,{'format':1,'site':self.app.site,'sender':self.app.node,'to':sender,
                                 'kind':'session-ack','data':proof})
        def remember(tx):
            for old in matches:
                if old['ack']['payload']['data']['closer_epoch'] < epoch:
                    tx.drop('_session_delivery_ack',old['id'])
            if len(tx.rows('_session_delivery_ack')) >= 256:
                raise Fault('Unsettled receipt capacity reached',429)
            tx.put('_session_delivery_ack',digest(payload),dict(prior,id=digest(payload),ack=ack,owner_key=peer['key']))
        self.business.execute(remember)
        return ack

    def _import_and_ack(self, state, packet):
        cache_id = digest(packet['payload'])
        def apply(tx):
            prior = tx.get('_session_delivery_ack', cache_id)
            if prior:
                verify(prior['owner_key'], packet.get('signature'), packet['payload'])
                return prior['ack']
            if len(tx.rows('_session_delivery_ack')) >= 256:
                raise Fault('Unsettled delivery receipt capacity reached',429)
            tx.session_import = state['id']
            proof = import_package(tx, self.app, state, packet)
            self._release_scope(tx,state['id'])
            ack = signed(self.app.key, {'format': 1, 'site': self.app.site, 'sender': self.app.node,
                        'to': state.get('closer', state['owner']), 'kind': 'session-ack', 'data': proof})
            tx.put('_session_delivery_ack', cache_id, {'id': cache_id, 'session': state['id'],
                   'owner_key': state['peer_keys'][packet['payload']['sender']],
                   'ack': ack, 'authorization':{k:state[k] for k in ('id','owner','authorized_closers','successor_after','authorization_digest','peer_keys')},
                   'outcome_digest':proof['outcome_digest'], 'final_head':proof['final_head'],
                   'baseline':state['baselines'][self.app.node]})
            return ack
        return self.business.execute(apply)

    def _discard_received(self, session_id, superseded=False):
        def discard(tx):
            state = tx.get('_coordination', 'active')
            if state and state['id'] == session_id and (superseded or not state.get('outbox_ready')):
                if superseded:
                    for node in state['participants']:
                        tx.drop('_outbox',node)
                for kind, key in state['scope']:
                    tx.drop(kind, key)
                tx.drop('_coordination', 'pending')
                tx.drop('_coordination', 'active')
        self.store.execute(discard)
