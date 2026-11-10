"""Atomic local records and an optional etcd-backed shared journal.

All domain updates, receipts and pending operations for a command commit together.
The SQLite mirror is disposable in shared mode. A failed CAS never becomes local
authority. Offline work uses its own database and explicit scopes.
"""
import base64
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import ssl
import sys
import weakref
import threading
import time
import uuid
from urllib.request import Request, build_opener, HTTPSHandler
from urllib.parse import urlparse


log = logging.getLogger('biomerte.store')

# Disposable scalar projections; canonical values remain in records/journal.
LOOKUP_FIELDS = {'runs': ('state',), 'leases': ('state', 'task'), 'actions': ('lease', 'state'),
                 'assistant_background': ('state', 'settlement_checked'),
                 'assistant_learning_outbox': ('delivered',)}
LOOKUP_DEFAULTS = {('assistant_background', 'settlement_checked'): False,
                   ('assistant_learning_outbox', 'delivered'): False}


def lookup_value(kind, field, value):
    if field in value:
        return value[field]
    return LOOKUP_DEFAULTS[(kind, field)]


class Fault(Exception):
    def __init__(self, message, status=400):
        super(Fault, self).__init__(message)
        self.status = status


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def uid():
    return uuid.uuid4().hex


def b64(data):
    return base64.b64encode(data).decode('ascii')


def un64(value):
    return base64.b64decode(value.encode('ascii'), validate=True)


def mkdir(path):
    if not os.path.isdir(path):
        try:
            os.makedirs(path, 0o700)
        except OSError:
            if not os.path.isdir(path):
                raise


def text(value, label='name', maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Fault('Invalid ' + label)
    return value


class Records:
    def __init__(self, db, read_only=False):
        self.db = db
        self.read_only = read_only
        self.changes = {}

    def get(self, kind, key, default=None):
        row = self.db.execute('SELECT value FROM records WHERE kind=? AND id=?',
                              (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def need(self, kind, key):
        value = self.get(kind, key)
        if value is None:
            raise Fault('Unknown %s %s' % (kind, key), 404)
        return value

    def rows(self, kind):
        rows = self.db.execute('SELECT id,value FROM records WHERE kind=? ORDER BY id', (kind,))
        result = []
        for key, raw in rows:
            value = json.loads(raw)
            if self._execution_eligible(kind,key,value):
                result.append(value)
        return result

    def _execution_eligible(self, kind, key, value):
        if not getattr(self,'ownership_execution',False):
            return True
        from .ownership import execution_eligible
        return execution_eligible(self,kind,key,value)

    def matching(self, kind, field=None, values=None, states=None, lease=None, delivery=False):
        if not isinstance(field, str):
            if field is not None:
                states = field
            return self.matching_lifecycle(kind, states, lease, delivery)
        """Ordered records matching an indexed scalar, without decoding history."""
        if field not in LOOKUP_FIELDS.get(kind, ()):
            raise ValueError('Field is not indexed')
        if not values:
            return []
        placeholders = ','.join('?' for unused in values)
        # Dirty rows cover writes by older binaries or direct SQL. Never trust
        # an outdated projection, even before the next startup repairs it.
        sql = ("SELECT r.id,r.value FROM record_lookup l CROSS JOIN records r "
               "ON r.kind=l.kind AND r.id=l.id WHERE l.kind=? AND l.field=? "
               "AND l.value IN (" + placeholders + ") AND NOT EXISTS "
               "(SELECT 1 FROM record_lookup_dirty d WHERE d.kind=r.kind AND d.id=r.id) "
               "UNION SELECT r.id,r.value FROM record_lookup_dirty d CROSS JOIN records r "
               "ON r.kind=d.kind AND r.id=d.id WHERE d.kind=? ORDER BY 1")
        encoded_values = [encode(value) for value in values]
        args = [kind, field] + encoded_values + [kind]
        result = []
        for key, encoded in self.db.execute(sql, args):
            value = json.loads(encoded)
            try:
                selected = encode(lookup_value(kind, field, value)) in encoded_values
            except KeyError:
                selected = False
            if selected and self._execution_eligible(kind,key,value):
                result.append(value)
        return result

    def _index(self, kind, key, value):
        if kind not in LOOKUP_FIELDS:
            return
        self.db.execute('DELETE FROM record_lookup WHERE kind=? AND id=?', (kind, key))
        if value is not None:
            for field in LOOKUP_FIELDS[kind]:
                try:
                    selected = lookup_value(kind, field, value)
                except KeyError:
                    continue
                self.db.execute('INSERT INTO record_lookup VALUES (?,?,?,?)',
                                (kind, key, field, encode(selected)))
        self.db.execute('DELETE FROM record_lookup_dirty WHERE kind=? AND id=?', (kind, key))

    def _session_write_allowed(self, kind, key, value=None):
        from .ownership import write_allowed
        write_allowed(self, kind, key, value)
        if kind.startswith('_'):
            return
        reservation = self.get('_session_scope', digest([kind, key]))
        if reservation and getattr(self, 'session_import', None) != reservation['session']:
            raise Fault('Durable record reserved by active local coordination session', 409)

    def refresh_metadata(self, kind):
        for key, raw in self.db.execute("SELECT r.id,r.value FROM records r JOIN record_lifecycle m ON r.kind=m.kind AND r.id=m.id WHERE m.kind=? AND m.state='@dirty'", (kind,)):
            self.metadata(kind, key, json.loads(raw))

    def matching_lifecycle(self, kind, states=None, lease=None, delivery=False):
        if not self.read_only:
            self.refresh_metadata(kind)
        sql = "SELECT r.id,r.value,m.state FROM records r LEFT JOIN record_lifecycle m ON m.kind=r.kind AND m.id=r.id WHERE r.kind=?"
        args = [kind]
        checks = []
        if states:
            checks.append("m.state IN (" + ','.join('?' for _ in states) + ")")
            args.extend(states)
        if lease is not None:
            checks.append("m.lease=?")
            args.append(lease)
        if delivery:
            checks.append("m.delivery=1")
        if checks:
            sql += " AND (m.state IS NULL OR m.state='@dirty' OR (" + " AND ".join(checks) + "))"
        result = []
        for key, raw, unused in self.db.execute(sql + " ORDER BY r.id", args):
            value = json.loads(raw)
            if states and value.get('state') not in states:
                continue
            if lease is not None and value.get('lease', value.get('destination', '')) != lease:
                continue
            if delivery and not (value.get('state') in ('succeeded','failed','abandoned') and value.get('destination') and not value.get('destination_delivered')):
                continue
            if self._execution_eligible(kind, key, value):
                result.append(value)
        return result

    def expired_keys(self, kind, now, limit=256):
        self.refresh_metadata(kind)
        return [r[0] for r in self.db.execute(
            "SELECT id FROM record_lifecycle WHERE kind=? AND expires_at<=? ORDER BY expires_at,id LIMIT ?",
            (kind, now, limit))]

    def metadata(self, kind, key, value):
        if not isinstance(value, dict):
            self.db.execute("INSERT OR REPLACE INTO record_lifecycle VALUES(?,?,'','',NULL,0)", (kind, key))
            return
        expires = value.get('expires_at')
        if expires is None and kind == 'actions' and value.get('state') in ('succeeded','failed','abandoned'):
            expires = time.time() + 900
        if expires is None and kind == 'leases' and value.get('state') == 'released':
            expires = time.time() + 300
        if expires is None and kind == 'guards' and not value.get('holder'):
            expires = time.time() + 300
        if kind == 'guards' and not value.get('holder') and value.get('released_at') is not None:
            expires = value['released_at'] + 300
        state = value.get('state', '')
        link = value.get('lease', value.get('destination', ''))
        if not isinstance(state, str):state = ''
        if not isinstance(link, str):link = ''
        if isinstance(expires, bool) or not isinstance(expires, (int,float)) or not __import__('math').isfinite(expires):expires = None
        self.db.execute("INSERT OR REPLACE INTO record_lifecycle VALUES(?,?,?,?,?,?)",
                        (kind, key, state, link, expires,
                         int(kind == 'actions' and value.get('state') in ('succeeded','failed','abandoned') and
                             bool(value.get('destination')) and not value.get('destination_delivered'))))

    def put(self, kind, key, value):
        if self.read_only:
            raise Fault(self.read_only, 409)
        text(key, 'record id', 512)
        self._session_write_allowed(kind, key, value)
        if kind == 'actions' and value.get('state') in ('succeeded', 'failed', 'abandoned') and not value.get('expires_at'):
            value['expires_at'] = time.time() + 900
        self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',
                        (kind, key, encode(value)))
        self.metadata(kind, key, value)
        self._index(kind, key, value)
        self.changes[(kind, key)] = value
        return value

    def drop(self, kind, key):
        if self.read_only:
            raise Fault(self.read_only, 409)
        self._session_write_allowed(kind, key)
        self.db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, key))
        self.db.execute('DELETE FROM record_lifecycle WHERE kind=? AND id=?', (kind, key))
        self._index(kind, key, None)
        self.changes[(kind, key)] = None

    def emit(self, topic, payload):
        counter = self.get('_meta', 'events', 0) + 1
        self.put('_meta', 'events', counter)
        event = {'id': uid(), 'seq': counter, 'topic': topic, 'payload': payload}
        if topic.startswith(('lease.', 'operation.', 'offer.')):
            event['expires_at'] = time.time() + 3600
        return self.put('events', '%020d' % counter, event)


class Etcd:
    """etcd v3 JSON gateway. The cluster operates membership and elections."""

    def __init__(self, settings, site):
        self.urls = settings['endpoints']
        if not self.urls:
            raise Fault('etcd requires endpoints')
        for url in self.urls:
            parsed = urlparse(url)
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and
                                                 parsed.hostname in ('localhost', '127.0.0.1', '::1')):
                raise Fault('Use TLS for remote etcd endpoints')
        self.prefix = '/biomerte/' + text(site, 'site') + '/'
        self.context = ssl.create_default_context(cafile=settings.get('ca') or None)
        if settings.get('cert'):
            self.context.load_cert_chain(settings['cert'], settings['key'])
        self.auth = settings.get('token', '')
        self.timeout = settings.get('timeout', 4)

    def rpc(self, path, payload):
        return self.gateway('kv/' + path, payload, self.urls)

    def gateway(self, path, payload, endpoints):
        errors = []
        for url in endpoints:
            req = Request(url.rstrip('/') + '/v3/' + path, encode(payload).encode(),
                          {'Content-Type': 'application/json', 'Authorization': self.auth})
            try:
                with build_opener(HTTPSHandler(context=self.context)).open(req, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode())
                if 'error' in result:
                    raise ValueError(result['error'])
                return result
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        raise Fault('Shared journal unavailable: ' + '; '.join(errors), 503)

    def key(self, name):
        return b64((self.prefix + name).encode())

    def read(self, name):
        result = self.rpc('range', {'key': self.key(name)})
        rows = result.get('kvs', [])
        if not rows:
            return None, '0'
        return json.loads(un64(rows[0]['value']).decode()), rows[0]['mod_revision']

    def append(self, expected, entry):
        number = entry['seq']
        head = {'seq': number, 'hash': digest(entry)}
        compare = {'key': self.key('head'), 'result': 'EQUAL'}
        if expected == '0':
            compare.update(target='VERSION', version='0')
        else:
            compare.update(target='MOD', mod_revision=expected)

        def put(key, value):
            return {'requestPut': {'key': self.key(key), 'value': b64(encode(value).encode())}}
        key = 'log/%020d' % number
        result = self.rpc('txn', {'compare': [compare], 'success': [
            put(key, entry), put('head', head)], 'failure': []})
        if not result.get('succeeded'):
            raise Fault('Shared head advanced. Retry with the same request ID.', 409)
        return head


_CONNECTIONS_LOCK = threading.RLock()
_IDLE_CONNECTIONS = []


def _close_connections(key):
    with _CONNECTIONS_LOCK:
        idle = [db for owner, db in _IDLE_CONNECTIONS if owner is key]
        _IDLE_CONNECTIONS[:] = [(owner, db) for owner, db in _IDLE_CONNECTIONS if owner is not key]
    for db in idle:
        db.close()


class Store:
    def __init__(self, directory, realm, remote=None):
        mkdir(directory)
        self.path = os.path.join(directory, realm + '.sqlite')
        self.remote = remote
        self._connection_key = object()
        self._connection_finalizer = weakref.finalize(self, _close_connections, self._connection_key)
        self._writer_diagnostic = None
        self._writer_diagnostic_lock = threading.Lock()
        with contextlib.closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,value TEXT,'
                             ' PRIMARY KEY(kind,id)); CREATE TABLE IF NOT EXISTS position('
                             'id INTEGER PRIMARY KEY CHECK(id=1),seq INTEGER,hash TEXT);'
                             "INSERT OR IGNORE INTO position VALUES(1,0,'');")
            db.executescript('CREATE TABLE IF NOT EXISTS record_lifecycle(kind TEXT,id TEXT,state TEXT,lease TEXT,expires_at REAL,delivery INTEGER, PRIMARY KEY(kind,id));'
                             'CREATE INDEX IF NOT EXISTS lifecycle_state ON record_lifecycle(kind,state,id);'
                             'CREATE INDEX IF NOT EXISTS lifecycle_lease ON record_lifecycle(kind,lease,id);'
                             'CREATE INDEX IF NOT EXISTS lifecycle_expiry ON record_lifecycle(kind,expires_at,id);'
                             'CREATE INDEX IF NOT EXISTS lifecycle_delivery ON record_lifecycle(kind,delivery,id);')
            db.executescript("CREATE TRIGGER IF NOT EXISTS lifecycle_insert AFTER INSERT ON records BEGIN INSERT OR REPLACE INTO record_lifecycle VALUES(NEW.kind,NEW.id,'@dirty','',NULL,0); END;"
                             "CREATE TRIGGER IF NOT EXISTS lifecycle_update AFTER UPDATE ON records BEGIN INSERT OR REPLACE INTO record_lifecycle VALUES(NEW.kind,NEW.id,'@dirty','',NULL,0); END;"
                             "CREATE TRIGGER IF NOT EXISTS lifecycle_delete AFTER DELETE ON records BEGIN DELETE FROM record_lifecycle WHERE kind=OLD.kind AND id=OLD.id; END;")
            db.execute('BEGIN IMMEDIATE')
            # Local derived index only: replay still goes through Records.put/drop.
            # Stream rows instead of materializing historical result bodies.
            tx = Records(db)
            for kind, key, raw in db.execute("SELECT r.kind,r.id,r.value FROM records r LEFT JOIN record_lifecycle m ON r.kind=m.kind AND r.id=m.id WHERE m.id IS NULL"):
                value = json.loads(raw)
                tx.metadata(kind, key, value)
            db.commit()

            self._initialize_lookup(db)
            from .retention_index import initialize
            initialize(db)
            db.commit()

    def _initialize_lookup(self, db):
        # One transactional backfill per mirror, then only dirty records on
        # startup. Triggers keep downgrade/direct-SQL writes detectable.
        schema = [
            'CREATE TABLE IF NOT EXISTS record_lookup('
            'kind TEXT,id TEXT,field TEXT,value TEXT,PRIMARY KEY(kind,field,value,id))',
            'CREATE INDEX IF NOT EXISTS record_lookup_id ON record_lookup(kind,id)',
            'CREATE TABLE IF NOT EXISTS record_lookup_dirty('
            'kind TEXT,id TEXT,PRIMARY KEY(kind,id))',
            'CREATE TABLE IF NOT EXISTS record_lookup_version(version INTEGER PRIMARY KEY)']
        kinds = ','.join("'" + kind + "'" for kind in sorted(LOOKUP_FIELDS))
        for event, prefix in (('INSERT', 'NEW'), ('DELETE', 'OLD'), ('UPDATE', 'OLD')):
            schema.append('CREATE TRIGGER IF NOT EXISTS record_lookup_v2_' + event.lower() +
                          ' AFTER ' + event + ' ON records WHEN ' + prefix + '.kind IN (' + kinds + ') '
                          'BEGIN INSERT OR REPLACE INTO record_lookup_dirty VALUES (' +
                          prefix + '.kind,' + prefix + '.id); END')
        schema.append('CREATE TRIGGER IF NOT EXISTS record_lookup_v2_update_new AFTER UPDATE ON records '
                      'WHEN NEW.kind IN (' + kinds + ') BEGIN INSERT OR REPLACE INTO record_lookup_dirty '
                      'VALUES (NEW.kind,NEW.id); END')
        try:
            # Execute the DDL inside SQL BEGIN, avoiding Python <3.6 cursor DDL
            # implicit-commit behavior. Data backfill stays in this transaction.
            db.executescript('BEGIN IMMEDIATE;' + ';'.join(schema) + ';')
            if not db.execute('SELECT 1 FROM record_lookup_version WHERE version=1').fetchone():
                db.execute('INSERT OR IGNORE INTO record_lookup_dirty SELECT kind,id FROM records '
                           'WHERE kind IN (' + kinds + ')')
            if not db.execute('SELECT 1 FROM record_lookup_version WHERE version=2').fetchone():
                db.execute("INSERT OR IGNORE INTO record_lookup_dirty SELECT kind,id FROM records "
                           "WHERE kind IN ('assistant_background','assistant_learning_outbox')")
            dirty = db.execute('SELECT kind,id FROM record_lookup_dirty').fetchall()
            tx = Records(db)
            for kind, key in dirty:
                tx._index(kind, key, tx.get(kind, key))
            db.execute('INSERT OR IGNORE INTO record_lookup_version VALUES(1)')
            db.execute('INSERT OR IGNORE INTO record_lookup_version VALUES(2)')
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def connect(self):
        con = sqlite3.connect(self.path, timeout=12, isolation_level=None, check_same_thread=False)
        con.execute('PRAGMA synchronous=FULL')
        return con


    @contextlib.contextmanager
    def connection(self):
        """Lease one warm connection exclusively, with a fresh transaction.

        Opening a connection and loading the schema for every request contends
        with SQLite 3.8 WAL I/O on the Pi. Reuse connections, never snapshots.
        The process-wide idle bound limits handles and page caches even when
        applications form reference cycles; concurrent requests remain unbounded.
        """
        db = None
        with _CONNECTIONS_LOCK:
            for index in range(len(_IDLE_CONNECTIONS) - 1, -1, -1):
                if _IDLE_CONNECTIONS[index][0] is self._connection_key:
                    unused, db = _IDLE_CONNECTIONS.pop(index)
                    break
        if db is None:
            db = self.connect()
        try:
            yield db
        finally:
            reusable = False
            try:
                db.rollback()
                db.execute('PRAGMA query_only=OFF')
                reusable = True
            except sqlite3.Error:
                pass
            evicted = None
            if reusable:
                with _CONNECTIONS_LOCK:
                    _IDLE_CONNECTIONS.append((self._connection_key, db))
                    if len(_IDLE_CONNECTIONS) > 8:
                        unused, evicted = _IDLE_CONNECTIONS.pop(0)
                db = None
            if db is not None:
                db.close()
            if evicted is not None:
                evicted.close()

    def close(self):
        """Release idle connections; checked-out operations retain ownership."""
        _close_connections(self._connection_key)

    def _report_writer_timeout(self):
        # Diagnostic only: never emit record values, callback arguments or locals.
        try:
            with self._writer_diagnostic_lock:
                owner = self._writer_diagnostic
                snapshot = tuple(owner) if owner is not None else None
            if snapshot is None:
                log.warning('SQLite writer timeout database=%s owner=untracked',
                            os.path.basename(self.path))
                return
            ident, phase, started = snapshot
            frame = sys._current_frames().get(ident)
            stack = []
            while frame is not None and len(stack) < 16:
                stack.append('%s:%s:%s' % (os.path.basename(frame.f_code.co_filename),
                                          frame.f_lineno, frame.f_code.co_name))
                frame = frame.f_back
            with self._writer_diagnostic_lock:
                unchanged = self._writer_diagnostic is owner
            if not unchanged:
                log.warning('SQLite writer timeout database=%s owner_changed_during_snapshot',
                            os.path.basename(self.path))
                return
            log.warning('SQLite writer timeout database=%s owner=%s phase=%s held_seconds=%.3f stack=%s',
                        os.path.basename(self.path), ident, phase,
                        time.monotonic() - started, ' <- '.join(stack))
        except Exception:
            # Reporting must not replace the original SQLite error.
            pass

    def _clear_writer_diagnostic(self, owner):
        # A successor can acquire the lock immediately after commit releases it.
        with self._writer_diagnostic_lock:
            if self._writer_diagnostic is owner:
                self._writer_diagnostic = None

    def execute(self, fn, cached=False, read_only=False):
        from .gateway_journal import JournalConflict
        while True:
            try:
                return self._execute(fn, cached, read_only)
            except JournalConflict:
                # A competing committed transaction won. Re-read its receipt and
                # re-evaluate against that history instead of replaying old changes.
                time.sleep(0.01)

    def _execute(self, fn, cached=False, read_only=False):
        from .gateway_journal import JournalConflict, JournalReplay
        with self.connection() as db:
            entries = {}
            checkpoint = None
            if self.remote and not cached:
                observed = db.execute('SELECT seq FROM position WHERE id=1').fetchone()[0]
                head, version = self.remote.read('head')
                last = head['seq'] if head else 0
                if last < observed:
                    raise Fault('Shared journal rolled back. Inspect membership and backup.', 409)
                if hasattr(self.remote, 'checkpoint'):
                    checkpoint = self.remote.checkpoint(observed, last, directory=os.path.dirname(self.path))
                start = checkpoint['seq'] if checkpoint else observed
                if start < last:
                    entries = JournalReplay(os.path.dirname(self.path))
                for number in range(start + 1, last + 1):
                    if checkpoint and (number - start) % 32 == 1:
                        checkpoint.renew()
                    entries[number], unused = self.remote.read('log/%020d' % number)
                if checkpoint:
                    checkpoint.release()
            if cached or read_only:
                # A WAL reader must not reserve the writer lock just to inspect
                # committed state while another caller waits for journal append.
                db.execute('BEGIN')
                seq, previous = db.execute('SELECT seq,hash FROM position WHERE id=1').fetchone()
                if self.remote and not cached and seq > last:
                    db.rollback()
                    raise JournalConflict('Local mirror advanced during catch-up', 409)
                if not self.remote or cached or seq == last:
                    try:
                        if self.remote and not cached and previous != (head['hash'] if head else ''):
                            raise Fault('Shared journal history mismatch', 409)
                        db.execute('PRAGMA query_only=ON')
                        result = fn(Records(db, 'Cached site view is read-only' if cached
                                            else 'Read-only operation changed records'))
                        db.commit()
                        return result
                    except BaseException:
                        db.rollback()
                        raise
                # Fresh reads still apply and validate missing journal entries.
                # End the snapshot before acquiring a write transaction.
                db.rollback()
            # Network catch-up never holds the mirror's writer lock. Another
            # local caller may already have applied part of this same history.
            try:
                db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as exc:
                if str(exc) == 'database is locked':
                    self._report_writer_timeout()
                raise
            owner = [threading.get_ident(), 'replay', time.monotonic()]
            with self._writer_diagnostic_lock:
                self._writer_diagnostic = owner
            try:
                tx = Records(db)
                seq, previous = db.execute('SELECT seq,hash FROM position WHERE id=1').fetchone()
                if self.remote and not cached:
                    if last < seq:
                        raise JournalConflict('Local mirror advanced during catch-up', 409)
                    if checkpoint and seq < checkpoint['seq']:
                        # Rebuild canonical state and disposable indexes in the
                        # same transaction as the verified position and tail.
                        db.execute('DELETE FROM records')
                        db.execute('DELETE FROM record_lookup')
                        db.execute('DELETE FROM record_lookup_dirty')
                        for kind, key, value in checkpoint.records():
                            tx.put(kind, key, value)
                            tx.changes.clear()  # authoritative restore, not a proposed write
                        seq, previous = checkpoint['seq'], checkpoint['hash']
                    elif checkpoint and seq == checkpoint['seq'] and previous != checkpoint['hash']:
                        raise Fault('Shared checkpoint history mismatch', 409)
                    while seq < last:
                        entry = entries[seq + 1]
                        if not entry or entry['prev'] != previous or entry['seq'] != seq + 1:
                            raise Fault('Shared journal discontinuity', 503)
                        for kind, key, value in entry['changes']:
                            if value is None:
                                tx.drop(kind, key)
                            else:
                                tx.put(kind, key, value)
                        tx.changes.clear()  # do not accumulate the fetched canonical suffix
                        seq += 1
                        previous = digest(entry)
                    if previous != (head['hash'] if head else ''):
                        raise Fault('Shared journal history mismatch', 409)
                    db.execute('UPDATE position SET seq=?,hash=? WHERE id=1', (seq, previous))
                    tx.changes.clear()
                if read_only:
                    # Catch-up is already authoritative. Release its writer lock
                    # before callbacks (which may perform remote status reads).
                    owner[1] = 'commit-catchup'
                    db.commit()
                    self._clear_writer_diagnostic(owner)
                    db.execute('BEGIN')
                    db.execute('SELECT seq FROM position WHERE id=1').fetchone()
                    db.execute('PRAGMA query_only=ON')
                    tx = Records(db, 'Read-only operation changed records')
                if self.remote and not cached and not read_only:
                    db.execute('SAVEPOINT proposal')
                owner[1] = 'callback'
                tx.journal_sequence = seq + 1 if self.remote and not cached and not read_only else None
                result = fn(tx)
                if (cached or read_only) and tx.changes:
                    raise Fault('Read-only operation changed records', 409)
                if self.remote and tx.changes:
                    owner[1] = 'encode-proposal'
                    changes = [[k, i, v] for (k, i), v in sorted(tx.changes.items())]
                    entry = {'seq': seq + 1, 'prev': previous, 'changes': changes}
                    encoded_entry = encode(entry)
                    expected_head = {'seq': entry['seq'], 'hash': hashlib.sha256(
                        encoded_entry.encode('utf-8')).hexdigest()}
                    # Only validated catch-up may commit locally before CAS.
                    # The proposal and its receipt are not yet authoritative.
                    owner[1] = 'commit-catchup'
                    db.execute('ROLLBACK TO proposal')
                    db.execute('RELEASE proposal')
                    db.commit()
                    self._clear_writer_diagnostic(owner)
                    # A slow or lost response must never pin the mirror writer.
                    head = self.remote.append(version, entry)
                    if (not isinstance(head, dict) or type(head.get('seq')) is not int or
                            not isinstance(head.get('hash'), str) or
                            head['seq'] != expected_head['seq'] or head['hash'] != expected_head['hash']):
                        raise Fault('Shared journal append acknowledgement mismatch', 503)

                    # The verified CAS acknowledgement makes this exact entry
                    # authoritative. Apply it without fetching head and our own
                    # log entry again. Never hold the writer lock across append.
                    entry = json.loads(encoded_entry)
                    try:
                        db.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError as exc:
                        if str(exc) == 'database is locked':
                            self._report_writer_timeout()
                        raise
                    owner = [threading.get_ident(), 'replay-acknowledged', time.monotonic()]
                    with self._writer_diagnostic_lock:
                        self._writer_diagnostic = owner
                    applied, anchor = db.execute(
                        'SELECT seq,hash FROM position WHERE id=1').fetchone()
                    if applied < entry['seq']:
                        if applied != entry['seq'] - 1 or anchor != entry['prev']:
                            raise Fault('Shared journal discontinuity', 503)
                        tx = Records(db)
                        for kind, key, value in entry['changes']:
                            if value is None:
                                tx.drop(kind, key)
                            else:
                                tx.put(kind, key, value)
                        db.execute('UPDATE position SET seq=?,hash=? WHERE id=1',
                                   (entry['seq'], expected_head['hash']))
                    elif applied == entry['seq'] and anchor != expected_head['hash']:
                        raise Fault('Shared journal history mismatch', 409)
                    # A caller already at or beyond this entry has replayed a
                    # validated journal chain. Do not overwrite its newer state.
                owner[1] = 'commit'
                db.commit()
                return result
            except BaseException:
                owner[1] = 'rollback'
                db.rollback()
                raise
            finally:
                self._clear_writer_diagnostic(owner)
