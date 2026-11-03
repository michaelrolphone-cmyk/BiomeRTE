"""Atomic local records and an optional etcd-backed shared journal.

All domain updates, receipts and pending operations for a command commit together.
The SQLite mirror is disposable in shared mode. A failed CAS never becomes local
authority. Offline work uses its own database and explicit scopes.
"""
import base64
import contextlib
import hashlib
import json
import os
import sqlite3
import ssl
import time
import uuid
from urllib.request import Request, build_opener, HTTPSHandler
from urllib.parse import urlparse


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
    def __init__(self, db):
        self.db = db
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
        rows = self.db.execute('SELECT value FROM records WHERE kind=? ORDER BY id', (kind,))
        return [json.loads(row[0]) for row in rows]

    def put(self, kind, key, value):
        text(key, 'record id', 512)
        self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',
                        (kind, key, encode(value)))
        self.changes[(kind, key)] = value
        return value

    def drop(self, kind, key):
        self.db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, key))
        self.changes[(kind, key)] = None

    def emit(self, topic, payload):
        counter = self.get('_meta', 'events', 0) + 1
        self.put('_meta', 'events', counter)
        event = {'id': uid(), 'seq': counter, 'topic': topic, 'payload': payload}
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


class Store:
    def __init__(self, directory, realm, remote=None):
        mkdir(directory)
        self.path = os.path.join(directory, realm + '.sqlite')
        self.remote = remote
        with contextlib.closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,value TEXT,'
                             ' PRIMARY KEY(kind,id)); CREATE TABLE IF NOT EXISTS position('
                             'id INTEGER PRIMARY KEY CHECK(id=1),seq INTEGER,hash TEXT);'
                             "INSERT OR IGNORE INTO position VALUES(1,0,'');")

    def connect(self):
        con = sqlite3.connect(self.path, timeout=12, isolation_level=None)
        con.execute('PRAGMA synchronous=FULL')
        return con

    def execute(self, fn, cached=False):
        with contextlib.closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                tx = Records(db)
                seq, previous = db.execute('SELECT seq,hash FROM position WHERE id=1').fetchone()
                if self.remote and not cached:
                    head, version = self.remote.read('head')
                    last = head['seq'] if head else 0
                    if last < seq:
                        raise Fault('Shared journal rolled back. Inspect membership and backup.', 409)
                    while seq < last:
                        entry, unused = self.remote.read('log/%020d' % (seq + 1))
                        if not entry or entry['prev'] != previous or entry['seq'] != seq + 1:
                            raise Fault('Shared journal discontinuity', 503)
                        for kind, key, value in entry['changes']:
                            if value is None:
                                tx.drop(kind, key)
                            else:
                                tx.put(kind, key, value)
                        seq += 1
                        previous = digest(entry)
                    if previous != (head['hash'] if head else ''):
                        raise Fault('Shared journal history mismatch', 409)
                    db.execute('UPDATE position SET seq=?,hash=? WHERE id=1', (seq, previous))
                    tx.changes.clear()
                result = fn(tx)
                if cached and tx.changes:
                    raise Fault('Cached site view is read-only', 409)
                if self.remote and tx.changes:
                    changes = [[k, i, v] for (k, i), v in sorted(tx.changes.items())]
                    entry = {'seq': seq + 1, 'prev': previous, 'changes': changes}
                    if len(encode(entry).encode()) > 900000:
                        raise Fault('Command exceeds shared transaction size. Split the work.', 413)
                    head = self.remote.append(version, entry)
                    db.execute('UPDATE position SET seq=?,hash=? WHERE id=1',
                               (head['seq'], head['hash']))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise
