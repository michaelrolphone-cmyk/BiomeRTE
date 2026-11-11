"""Finite, signed responsibility handoff between independent local stores.

The registry contains current ownership, not a shared execution history. A
handoff freezes local writes before exporting a bounded exact snapshot. Only a
verified, durably staged recipient can become owner. Lost replies never clear a
fence; expiry is not permission to take over. Local temporary payloads disappear
only after both participants have reconciled the committed epoch.

This is an operator control plane. It is deliberately not an ordinary domain
operation or an automatic peer-enrollment route.
"""
import json
import time
import uuid
import os
import stat
import fcntl
import threading
import re
from functools import wraps
from .store import Fault, digest, encode
from .identity import fingerprint, signed, verify
from .retention import strings, terminal_safe


CURRENT = '_ownership_current'
PENDING = '_ownership_pending'
INERT = frozenset(('contracts', '_contract_usage', '_builtin_contracts',
                   'definitions', 'process_versions', 'recipes', 'documents',
                   'revisions', '_writers', 'artifacts'))
AUTHORITY = frozenset(('peers', 'authorization_grants', 'grants'))
CHURN = frozenset(('actions', 'leases', '_receipt', '_receipt_stream', '_retention'))
LOCAL_META = frozenset(('events','last_run','initialized','operational_retention','delegation_sequence'))
BINDING = ('unit', 'epoch', 'owner', 'target', 'transfer', 'authority_digest',
           'scope', 'aliases', 'snapshot_digest', 'authorization_digest', 'expires')
DEFAULT_LIMITS = {'scope':256, 'bytes':16 * 1024 * 1024, 'row_bytes':1024 * 1024,
                  'scan_rows':100000, 'scan_bytes':512 * 1024 * 1024, 'seconds':86400}
METADATA_BYTES = 65536


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._mutex:
            if self._depth:
                return method(self, *args, **kwargs)
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise Fault('Ownership control lock must be private and regular', 403)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    return method(self, *args, **kwargs)
                finally:
                    self._depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    return call


def identifier(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def key_identifier(value):
    return isinstance(value,str) and re.match(r'^[A-Za-z0-9_.:-]{1,128}\Z',value) is not None


def bounded_text(value, maximum=512):
    return (isinstance(value,str) and bool(value) and len(value.encode('utf-8')) <= maximum and
            not any(ord(c) < 32 or ord(c) == 127 for c in value))


def metadata_admitted(value):
    # The transport uses this canonical JSON encoding. It is conservative for
    # non-ASCII text compared with JS UTF-8 and includes attestation overhead.
    if len(encode(value).encode('utf-8')) > METADATA_BYTES:
        raise Fault('Ownership metadata exceeds the registry admission bound',413)


def write_allowed(tx, kind, key, value=None):
    """Enforce both the temporary fence and current foreign responsibility.

    Called by Records, including internal/derived namespace writes. This is not
    bypassed by an OAuth operator call or by creating a fresh record ID.
    """
    if getattr(tx, 'ownership_control', False):
        return
    if kind in (CURRENT, PENDING):
        raise Fault('Ownership metadata requires its explicit control plane', 403)
    if kind == '_meta' and key in LOCAL_META:
        return
    refs = None
    pending = tx.get(PENDING, 'active')
    if pending and pending.get('phase') not in ('reconciled', 'adoption_pending'):
        refs = set(strings(value)) if value is not None else set()
        refs.add(key)
        old = tx.get(kind, key)
        if old is not None:
            refs.update(strings(old))
        reserved = {ref[1] for ref in pending['scope']} | set(pending['aliases'])
        if [kind,key] in pending['scope'] or refs.intersection(reserved):
            raise Fault('Responsibility records are fenced pending reconciliation', 409)
    if kind in INERT:
        return  # Knowledge copies carry no right to schedule or execute work.
    for (raw,) in tx.db.execute('SELECT value FROM records WHERE kind=?', (CURRENT,)):
        current = json.loads(raw)
        if current['owner'] == current['local']:
            continue
        if refs is None:
            refs = set(strings(value)) if value is not None else set()
            refs.add(key)
            old = tx.get(kind, key)
            if old is not None:
                refs.update(strings(old))
        if refs.intersection(current['aliases']):
            raise Fault('Record belongs to another current responsibility owner', 409)


def execution_eligible(tx, kind, key, value):
    try:
        write_allowed(tx,kind,key,value)
        return True
    except Fault as error:
        if error.status in (403,409):
            return False
        raise


def _scope(scope, limits):
    if not isinstance(scope, list) or not 0 < len(scope) <= limits['scope']:
        raise Fault('Explicit bounded ownership scope required', 413)
    result = set()
    for pair in scope:
        if (not isinstance(pair, list) or len(pair) != 2 or
                not key_identifier(pair[0]) or not bounded_text(pair[1]) or
                pair[0].startswith(('_ownership', '_session', '_coordination'))):
            raise Fault('Invalid ownership scope')
        if tuple(pair) in result:
            raise Fault('Duplicate ownership scope')
        result.add(tuple(pair))
    return sorted(result)


def _aliases(aliases):
    if (not isinstance(aliases, list) or not 0 < len(aliases) <= 128 or
            len(set(aliases)) != len(aliases) or
            any(not bounded_text(x) for x in aliases)):
        raise Fault('Explicit unique responsibility aliases required')
    return sorted(aliases)


def snapshot(tx, scope, aliases, limits=None):
    """Canonical bounded scan, independent of dirty disposable indexes.

    Any known record-ID edge crossing the executable scope rejects the plan.
    Inert dependencies must be included in the package too, but are copied, not
    removed from the source. Unknown namespaces are not assumed inert.
    """
    limits = dict(DEFAULT_LIMITS if limits is None else limits)
    scope = _scope(scope, limits)
    selected = set(scope)
    required_roots = {key for kind, key in selected if kind not in INERT | CHURN}
    if not required_roots.issubset(set(aliases)):
        raise Fault('Every current executable root requires an explicit stable owner alias', 409)
    ids, count, total = {}, 0, 0
    for kind, key, size in tx.db.execute('SELECT kind,id,length(CAST(value AS BLOB)) FROM records ORDER BY kind,id'):
        if kind in (CURRENT, PENDING) or kind == '_meta' and key in LOCAL_META:
            continue
        count += 1
        total += size
        if count > limits['scan_rows'] or total > limits['scan_bytes'] or size > limits['row_bytes']:
            raise Fault('Canonical ownership scan budget exceeded', 413)
        ids.setdefault(key, set()).add((kind, key))
    if any(ref not in ids.get(ref[1], ()) for ref in selected):
        raise Fault('Ownership scope contains a missing canonical record', 409)
    rows, used = [], 0
    selected_ids = {ref[1] for ref in selected} | set(aliases)
    for kind, key, raw in tx.db.execute('SELECT kind,id,value FROM records ORDER BY kind,id'):
        if kind in (CURRENT, PENDING) or kind == '_meta' and key in LOCAL_META:
            continue
        ref = (kind, key)
        value = json.loads(raw)
        refs = set(strings(value))
        if ref in selected:
            # No in-flight physical claim is transferable just because its
            # database rows are available. Pending durable work is preserved.
            if kind in ('leases', 'actions') and not terminal_safe(kind, value):
                raise Fault('Physical operation is not quiescent: ' + kind + '/' + key, 409)
            if kind == 'activations' and value.get('state') == 'pending':
                raise Fault('Provider activation requires physical reconciliation', 409)
            if kind == 'subscriptions' or kind.startswith('_session'):
                raise Fault('External delivery/session state requires separate reconciliation', 409)
            if kind in ('_meta', 'events', 'delegation_events'):
                raise Fault('Local counters and event delivery require separate reconciliation', 409)
            for item in refs:
                if any(other not in selected for other in ids.get(item, ())):
                    raise Fault('Ownership snapshot has an unselected canonical dependency', 409)
            used += len(raw.encode('utf-8'))
            if used > limits['bytes']:
                raise Fault('Ownership snapshot byte budget exceeded', 413)
            rows.append([kind, key, value])
        elif kind not in INERT and refs.intersection(selected_ids):
            raise Fault('Current executable record outside ownership scope depends on it', 409)
    return rows


class OwnershipRuntime:
    def __init__(self, app, remote, limits=None):
        self.app, self.remote = app, remote
        self.business = app.stores['site']
        self._mutex, self._depth = threading.RLock(), 0
        self._lock_path = self.business.path + '.ownership.lock'
        if fingerprint(app.config['public_key']) != app.node:
            raise Fault('Application identity must match its public signing key', 403)
        if self.business.remote is not None:
            raise Fault('Ownership transfer requires local SQLite, not a shared mirror', 409)
        self.limits = dict(DEFAULT_LIMITS)
        if limits:
            self.limits.update(limits)
        if any(type(v) is not int or v <= 0 for v in self.limits.values()):
            raise Fault('Finite positive ownership limits required')
        if self.limits['scope'] > 256 or self.limits['seconds'] > 86400:
            raise Fault('Ownership limits exceed the configured registry protocol')

    def _control(self, fn):
        def run(tx):
            tx.ownership_control = True
            return fn(tx)
        return self.business.execute(run)

    def state(self):
        return self.business.execute(lambda tx: tx.get(PENDING, 'active'), read_only=True)

    def _current(self, unit):
        return self.business.execute(lambda tx: tx.get(CURRENT, unit), read_only=True)

    def _public(self, current):
        return {k:current[k] for k in ('unit', 'owner', 'epoch', 'authority_digest', 'aliases')}

    def _binding(self, state):
        return {k:state[k] for k in BINDING}

    def _foreign_baseline(self, local, current):
        if local is None:
            return
        if (local.get('local') != self.app.node or local.get('owner') == self.app.node or
                type(local.get('epoch')) is not int or type(current.get('epoch')) is not int or
                local['epoch'] > current['epoch'] or
                local['epoch'] == current['epoch'] and local.get('owner') != current.get('owner') or
                any(local.get(k) != current.get(k) for k in ('authority_digest','aliases'))):
            raise Fault('Recipient has conflicting current local responsibility',409)

    def _update(self, expected, **fields):
        def change(tx):
            state = tx.need(PENDING, 'active')
            if state.get('transfer') != expected.get('transfer') or state.get('unit') != expected.get('unit'):
                raise Fault('Ownership intent changed concurrently', 409)
            state.update(fields)
            tx.put(PENDING, 'active', state)
            return state
        return self._control(change)

    @serialized
    def bootstrap(self, unit, aliases, authority_digest, scope, snapshot_digest):
        """Adopt only under an exact registry-side operator manifest gate.

        The registry cannot accept this from an ordinary request boolean. The
        deployment supplies the short-lived, exact bootstrap authorization.
        """
        aliases = _aliases(aliases)
        if not key_identifier(unit) or not identifier(authority_digest) or not identifier(snapshot_digest):
            raise Fault('Invalid responsibility identity')
        request = {'unit':unit,'owner':self.app.node,'aliases':aliases,'authority_digest':authority_digest,
                   'expected_epoch':0,'snapshot_digest':snapshot_digest,'local_authority':True,'durable':True}
        metadata_admitted(request)
        def freeze(tx):
            if tx.get(PENDING, 'active') or tx.get(CURRENT, unit):
                raise Fault('Reconcile existing ownership before bootstrap', 409)
            rows = snapshot(tx, scope, aliases, self.limits)
            if digest(rows) != snapshot_digest:
                raise Fault('Bootstrap snapshot differs from qualified adoption', 409)
            for raw, in tx.db.execute('SELECT value FROM records WHERE kind=?', (CURRENT,)):
                if set(json.loads(raw)['aliases']).intersection(aliases):
                    raise Fault('Responsibility aliases already owned locally', 409)
            state = {'unit':unit, 'owner':self.app.node, 'epoch':1, 'aliases':aliases,
                     'authority_digest':authority_digest, 'snapshot_digest':snapshot_digest,
                     'phase':'bootstrap', 'local':self.app.node, 'scope':[list(ref) for ref in _scope(scope,self.limits)]}
            tx.put(PENDING, 'active', state)
            return state
        state = self._control(freeze)
        self.remote('bootstrap', request)
        return self.reconcile()

    @serialized
    def adopt_current(self, unit, aliases, authority_digest, scope, snapshot_digest):
        """Register existing sole custody without moving or pausing its records.

        This initial-cutover operation requires the same exact gateway-side
        bootstrap manifest as bootstrap(). authority_digest must bind the
        qualified singular-owner migration plan, not physical device location.
        The bounded scope is a current-state witness, not a transfer package.
        It may contain active operations because none are copied or replayed.
        Subsequent prepare/stage retain their strict quiescence checks.
        """
        aliases = _aliases(aliases)
        scope = _scope(scope, self.limits)
        if not key_identifier(unit) or not identifier(authority_digest) or not identifier(snapshot_digest):
            raise Fault('Invalid responsibility identity')
        request = {'unit':unit,'owner':self.app.node,'aliases':aliases,'authority_digest':authority_digest,
                   'expected_epoch':0,'snapshot_digest':snapshot_digest,'local_authority':True,'durable':True}
        metadata_admitted(request)
        def record(tx):
            if tx.get(PENDING, 'active') or tx.get(CURRENT, unit):
                raise Fault('Reconcile existing ownership before adoption', 409)
            if tx.db.execute("SELECT 1 FROM records WHERE kind IN ('_session_scope','_coordination') LIMIT 1").fetchone():
                raise Fault('Local coordination must settle before adoption', 409)
            rows, witnesses, used = [], set(), 0
            for kind, key in scope:
                value = tx.need(kind,key)
                size = len(encode(value).encode('utf-8'))
                used += size
                if size > self.limits['row_bytes'] or used > self.limits['bytes']:
                    raise Fault('Adoption witness byte budget exceeded', 413)
                rows.append([kind,key,value])
                if kind not in INERT and kind != '_meta':
                    witnesses.add(key)
                    witnesses.update(strings(value))
            if not set(aliases).issubset(witnesses) or digest(rows) != snapshot_digest:
                raise Fault('Adoption witness differs from qualified current authority', 409)
            for raw, in tx.db.execute('SELECT value FROM records WHERE kind=?', (CURRENT,)):
                if set(json.loads(raw)['aliases']).intersection(aliases):
                    raise Fault('Responsibility aliases already owned locally', 409)
            state = {'unit':unit,'owner':self.app.node,'epoch':1,'aliases':aliases,
                     'authority_digest':authority_digest,'snapshot_digest':snapshot_digest,
                     'phase':'adoption_pending','local':self.app.node}
            # No snapshot, scope, lease or action body persists here. This does
            # not fence existing custody or confer authority on another node.
            tx.put(PENDING, 'active', state)
        self._control(record)
        self.remote('bootstrap', request)
        return self.reconcile()

    @serialized
    def prepare(self, unit, target_key, scope, snapshot_digest, recipient_baselines,
                expires, authorization_digest):
        target = fingerprint(target_key)
        if target == self.app.node or not identifier(authorization_digest):
            raise Fault('Distinct authorized recipient required')
        now = time.time()
        if type(expires) not in (int, float) or not now < expires <= now + self.limits['seconds']:
            raise Fault('Explicit finite transfer expiry required')
        scope = [list(ref) for ref in _scope(scope, self.limits)]
        if not isinstance(recipient_baselines, list) or len(recipient_baselines) != len(scope):
            raise Fault('Exact recipient baselines required')
        if any(not identifier(x) for x in recipient_baselines):
            raise Fault('Invalid recipient baseline')
        current = self._current(unit)
        if not current or current['owner'] != self.app.node:
            raise Fault('Only the current local owner can prepare a handoff', 409)
        if (not key_identifier(unit) or type(current.get('epoch')) is not int or
                not 1 <= current['epoch'] < 9007199254740991 or not identifier(snapshot_digest)):
            raise Fault('Invalid ownership transfer identity or epoch')
        transfer = uuid.uuid4().hex
        proof = dict(self._public(current), target=target, transfer=transfer, scope=scope,
                     snapshot_digest=snapshot_digest, authorization_digest=authorization_digest, expires=expires)
        metadata_admitted(dict(proof, source_frozen=True, verified=True, staged=True, durable=True))
        status = self.remote('status', {'unit':unit})
        if any(status.get(k) != v for k, v in self._public(current).items()) or status.get('transfer'):
            raise Fault('Registry ownership is not the current local baseline', 409)
        def freeze(tx):
            if tx.get(PENDING, 'active') or tx.get(CURRENT, unit) != current:
                raise Fault('Local ownership changed before freeze', 409)
            if tx.db.execute("SELECT 1 FROM records WHERE kind IN ('_session_scope','_coordination') LIMIT 1").fetchone():
                raise Fault('Local coordination must settle before ownership handoff', 409)
            rows = snapshot(tx, scope, current['aliases'], self.limits)
            if digest(rows) != snapshot_digest:
                raise Fault('Current snapshot differs from reviewed handoff', 409)
            state = dict(self._public(current), target=target, transfer=transfer,
                scope=scope, snapshot_digest=snapshot_digest, authorization_digest=authorization_digest,
                expires=expires, phase='source_frozen', records=rows, baselines=recipient_baselines,
                source_key=self.app.config['public_key'], target_key=target_key, local=self.app.node)
            tx.put(PENDING, 'active', state)
            return state
        state = self._control(freeze)
        self.remote('prepare', dict(self._binding(state), source_frozen=True))
        return self.package()

    @serialized
    def package(self):
        state = self.state()
        if not state or state.get('owner') != self.app.node or state['phase'] != 'source_frozen':
            raise Fault('No frozen source snapshot available', 409)
        data = dict(self._binding(state), records=state['records'], baselines=state['baselines'],
                    source_key=state['source_key'], target_key=state['target_key'])
        return signed(self.app.key, {'format':1, 'kind':'ownership-handoff', 'site':self.app.site,
                                    'sender':self.app.node, 'to':state['target'], 'data':data})

    @serialized
    def stage(self, packet, expected_source_key, authorized_packet_digest, approved_authority=None):
        """Exact operator-authorized package; never a new persistent peer grant.

        Changed peer/grant rows require their exact canonical hashes in the
        one-time approved_authority map. A caller must obtain any required trust
        approval before supplying it; this method does not create a peer scope.
        """
        if len(encode(packet).encode('utf-8')) > self.limits['bytes'] + self.limits['row_bytes']:
            raise Fault('Ownership packet byte budget exceeded', 413)
        if digest(packet) != authorized_packet_digest:
            raise Fault('Package differs from exact one-time authorization', 403)
        body = packet.get('payload', {})
        data = body.get('data', {})
        source = fingerprint(expected_source_key)
        if (body.get('format') != 1 or body.get('kind') != 'ownership-handoff' or
                body.get('site') != self.app.site or body.get('sender') != source or
                body.get('to') != self.app.node or data.get('owner') != source or
                data.get('target') != self.app.node or data.get('source_key') != expected_source_key or
                data.get('target_key') != self.app.config['public_key']):
            raise Fault('Ownership package destination or key pin mismatch', 403)
        verify(expected_source_key, packet.get('signature'), body)
        scope = _scope(data.get('scope'), self.limits)
        if ([row[:2] for row in data.get('records', [])] != [list(x) for x in scope] or
                any(not isinstance(row, list) or len(row) != 3 for row in data['records']) or
                digest(data['records']) != data.get('snapshot_digest') or
                len(data.get('baselines', [])) != len(scope)):
            raise Fault('Snapshot scope, digest or baseline mismatch', 409)
        aliases = _aliases(data.get('aliases'))
        proof = {k:data[k] for k in BINDING}
        if (not key_identifier(data['unit']) or not key_identifier(data['transfer']) or
                type(data['epoch']) is not int or not 1 <= data['epoch'] < 9007199254740991 or
                not identifier(data['authority_digest']) or not identifier(data['authorization_digest'])):
            raise Fault('Invalid signed ownership metadata')
        metadata_admitted(dict(proof,source_frozen=True,verified=True,staged=True,durable=True))
        status = self.remote('status', {'unit':data['unit']})
        receipt = status.get('transfer') or {}
        if any(receipt.get(k) != v for k, v in proof.items()) or receipt.get('state') not in ('prepared', 'ready'):
            raise Fault('Registry lacks the exact source freeze', 409)
        approved_authority = {} if approved_authority is None else approved_authority
        def stage(tx):
            prior = tx.get(PENDING, 'active')
            if prior:
                if prior.get('packet_digest') == authorized_packet_digest and prior['phase'] == 'target_staged':
                    return prior
                raise Fault('Another local ownership intent is unresolved', 409)
            # Existing executable recipient rows must not acquire a new foreign
            # resource implicitly merely because its ID arrived in a package.
            selected = set(scope)
            incoming_ids = {key for kind, key in scope} | set(aliases)
            count, total = 0, 0
            for kind, key, raw in tx.db.execute('SELECT kind,id,value FROM records ORDER BY kind,id'):
                count += 1
                size = len(raw.encode('utf-8'))
                total += size
                if count > self.limits['scan_rows'] or total > self.limits['scan_bytes'] or size > self.limits['row_bytes']:
                    raise Fault('Recipient canonical scan budget exceeded', 413)
                if ((kind, key) not in selected and kind not in INERT and kind not in (CURRENT, PENDING)
                        and set(strings(json.loads(raw))).intersection(incoming_ids)):
                    raise Fault('Recipient executable dependency is outside handoff scope', 409)
            for (kind, key, value), expected in zip(data['records'], data['baselines']):
                old = tx.get(kind, key)
                if old is not None and ((kind in ('actions','leases') and not terminal_safe(kind,old)) or
                                        kind == 'activations' and old.get('state') == 'pending'):
                    raise Fault('Recipient physical operation is not quiescent', 409)
                if digest(old) != expected:
                    raise Fault('Recipient baseline changed before staging', 409)
                if kind in AUTHORITY and digest(old) != digest(value):
                    if approved_authority.get(encode([kind, key])) != digest(value):
                        raise Fault('Exact changed authority requires explicit approval', 403)
            for raw, in tx.db.execute('SELECT value FROM records WHERE kind=?', (CURRENT,)):
                local = json.loads(raw)
                if local['unit'] == data['unit']:
                    self._foreign_baseline(local,status)
                if local['unit'] != data['unit'] and set(local['aliases']).intersection(aliases):
                    raise Fault('Recipient already owns overlapping responsibility', 409)
            state = dict(data, local=self.app.node, phase='target_staged', packet_digest=authorized_packet_digest)
            tx.put(PENDING, 'active', state)
            return state
        state = self._control(stage)
        self.remote('ready', dict(proof, verified=True, staged=True))
        return {'unit':state['unit'], 'transfer':state['transfer'], 'staged':True}

    @serialized
    def commit(self):
        state = self.state()
        if not state or state['phase'] not in ('source_frozen', 'target_staged'):
            raise Fault('No staged ownership transfer', 409)
        self.remote('commit', self._binding(state))
        return self.reconcile()

    @serialized
    def cancel(self):
        state = self.state()
        if not state or state.get('owner') != self.app.node or state['phase'] != 'source_frozen':
            raise Fault('Only the frozen source can request cancellation', 409)
        self.remote('cancel', self._binding(state))
        return self.reconcile()

    @serialized
    def acknowledge_cancelled(self, unit):
        """Recipient with no staged snapshot can close a cancelled intent.

        This is a read-before-ACK, not a takeover or import. If any local intent
        is present its normal reconciliation path must be used instead.
        """
        if self.state():
            raise Fault('Reconcile the existing local handoff first', 409)
        status = self.remote('status', {'unit':unit})
        receipt = status.get('transfer') or {}
        if receipt.get('state') != 'aborted' or receipt.get('target') != self.app.node:
            raise Fault('No cancelled recipient handoff to acknowledge', 409)
        expected = dict(self._public(receipt),epoch=receipt['epoch']+1)
        if any(status.get(k) != v for k,v in expected.items()):
            raise Fault('Cancelled responsibility does not match current authority',409)
        def refresh(tx):
            local = tx.get(CURRENT,unit)
            self._foreign_baseline(local,status)
            if local is not None:
                tx.put(CURRENT,unit,dict(expected,local=self.app.node))
        self._control(refresh)
        return self.remote('ack', dict(self._binding(receipt), durable=True))

    @serialized
    def resume(self):
        """Explicit exact-intent retry after inspecting a failed first send.

        Never rebuilds a snapshot or changes recipient, expiry or authorization.
        Invoking this after an authorization denial requires new authorization;
        it is not called by background maintenance.
        """
        state = self.state()
        if not state:
            raise Fault('No persisted ownership intent', 409)
        if state['phase'] in ('bootstrap', 'adoption_pending'):
            request = dict(self._public(state), expected_epoch=0, snapshot_digest=state['snapshot_digest'],
                           local_authority=True, durable=True)
            request.pop('epoch')
            self.remote('bootstrap', request)
            return self.reconcile()
        if state['phase'] == 'source_frozen':
            self.remote('prepare', dict(self._binding(state), source_frozen=True))
            return self.package()
        if state['phase'] == 'target_staged':
            self.remote('ready', dict(self._binding(state), verified=True, staged=True))
            return {'unit':state['unit'], 'staged':True}
        return self.reconcile()

    @serialized
    def reconcile(self):
        state = self.state()
        if not state:
            return {'pending':False}
        status = self.remote('status', {'unit':state['unit']})
        if state['phase'] in ('bootstrap', 'adoption_pending'):
            if any(status.get(k) != v for k, v in self._public(state).items()) or status.get('transfer'):
                raise Fault('Bootstrap outcome is not established; existing custody unchanged', 409)
            def adopt(tx):
                if tx.need(PENDING, 'active') != state:
                    raise Fault('Bootstrap changed concurrently', 409)
                tx.put(CURRENT, state['unit'], dict(self._public(state), local=self.app.node))
                tx.drop(PENDING, 'active')
            self._control(adopt)
            return {'adopted':state['unit'], 'epoch':1}
        receipt = status.get('transfer')
        aborted = (receipt or {}).get('state') == 'aborted' or state.get('outcome') == 'aborted'
        expected = dict(self._public(state), owner=state['owner'] if aborted else state['target'], epoch=state['epoch'] + 1)
        if any(status.get(k) != v for k, v in expected.items()):
            raise Fault('Ownership has not committed; local fence preserved', 409)
        if receipt and (receipt.get('state') != ('aborted' if aborted else 'committed') or
                        any(receipt.get(k) != v for k, v in self._binding(state).items())):
            raise Fault('Committed transfer proof mismatch; local fence preserved', 409)
        if state['phase'] != 'reconciled':
            if not receipt:
                raise Fault('Missing transfer proof before local reconciliation', 409)
            def apply(tx):
                current = tx.need(PENDING, 'active')
                if current != state:
                    raise Fault('Local transfer changed concurrently', 409)
                for index, (kind, key, value) in enumerate([] if aborted else state['records']):
                    if self.app.node == state['owner']:
                        if digest(tx.get(kind, key)) != digest(value):
                            raise Fault('Frozen source changed; preserve the fence', 409)
                        if kind not in INERT:
                            if kind == '_receipt':
                                # No result payload is retained at the old
                                # owner. Keep the existing bounded retry
                                # disposition under its normal retention policy.
                                retired = {field:value[field] for field in ('signature','format','operation','created_at') if field in value}
                                retired.update(ownership_owner=state['target'])
                                tx.put(kind, key, retired)
                            elif kind == '_receipt_stream':
                                tx.put(kind,key,{'sequence':value['sequence'],'signature':value['signature'],
                                                 'ownership_owner':state['target']})
                            elif kind == '_retention' and value.get('kind') == '_receipt':
                                pass  # Original bounded receipt window remains at its old owner.
                            else:
                                tx.drop(kind, key)
                    else:
                        if digest(tx.get(kind, key)) != state['baselines'][index]:
                            raise Fault('Frozen recipient changed; preserve the fence', 409)
                        tx.put(kind, key, value)
                tx.put(CURRENT, state['unit'], dict(expected, local=self.app.node))
                current['phase'] = 'reconciled'
                current['outcome'] = 'aborted' if aborted else 'committed'
                # Snapshot payloads are no longer needed after the durable
                # local transaction. Keep only the exact unresolved ACK proof.
                for key in ('records', 'baselines', 'source_key', 'target_key', 'packet_digest'):
                    current.pop(key, None)
                tx.put(PENDING, 'active', current)
            self._control(apply)
            state = self.state()
        if receipt:
            self.remote('ack', dict(self._binding(state), durable=True))
            status = self.remote('status', {'unit':state['unit']})
        if status.get('transfer') is None and all(status.get(k) == v for k, v in expected.items()):
            def finish(tx):
                if tx.need(PENDING, 'active') != state:
                    raise Fault('Local cleanup changed concurrently', 409)
                tx.drop(PENDING, 'active')
            self._control(finish)
            return {'pending':False, 'unit':state['unit'], 'owner':expected['owner'], 'epoch':expected['epoch'], 'outcome':state['outcome']}
        return {'pending':True, 'unit':state['unit'], 'phase':'reconciled'}
