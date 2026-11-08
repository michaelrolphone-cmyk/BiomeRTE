"""Shared, compare-and-swap journal through the authenticated MCP gateway."""
import hashlib
import json
import ssl
import os
import sqlite3
import tempfile
from urllib.request import Request, build_opener, HTTPSHandler
from urllib.error import HTTPError
from .store import Fault, encode


class JournalConflict(Fault):
    pass


class GatewayJournal:
    def __init__(self, settings, site):
        self.origin = settings['origin'].rstrip('/')
        if not self.origin.startswith('https://'):
            raise Fault('Gateway journal requires HTTPS')
        self.site = site
        self.key_file = settings['key_file']
        self.timeout = settings.get('network_timeout', 45)

    def request(self, path, payload):
        with open(self.key_file) as handle:
            token = handle.read().strip()
        request = Request(self.origin + '/bridge/ha/' + path,
                          encode(payload).encode('utf-8'),
                          {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                           'User-Agent': 'BiomeRTE-Controller-Link/1.0'})
        try:
            opener = build_opener(HTTPSHandler(context=ssl.create_default_context()))
            with opener.open(request, timeout=self.timeout) as response:
                if path.startswith('checkpoint'):
                    raw = response.read(5242881)
                    if len(raw) > 5242880:
                        raise Fault('Shared checkpoint response exceeds bound', 503)
                else:
                    raw = response.read()
                return json.loads(raw.decode('utf-8'))
        except HTTPError as exc:
            if exc.code == 413 and path == 'append':
                raise Fault('Shared journal transaction exceeds configured size limit; split the transaction before retrying', 413)
            if exc.code == 409 and path == 'append':
                raise JournalConflict('Shared head advanced. Retry the same request ID.', 409)
            raise Fault('Gateway journal HTTP ' + str(exc.code), 503)
        except (OSError, ValueError):
            raise Fault('Gateway journal unavailable', 503)

    def read(self, name):
        result = self.request('read', {'key': name})
        if name == 'head':
            self.checkpoint_hint = result.get('checkpoint')
        value = json.loads(result['encoded']) if result.get('encoded') is not None else result.get('value')
        return value, result.get('version', '0')

    def checkpoint(self, observed, maximum, directory=None):
        hint = getattr(self, 'checkpoint_hint', None)
        if not hint or hint.get('recovery_seq', hint['seq']) <= observed:
            return None
        result = self.request('checkpoint', {'max_seq': maximum})
        if result is None:
            return None
        encoded = self._checkpoint_bytes(result, 32768)
        state = json.loads(encoded)
        if (state.get('format') != 2 or state.get('site') != self.site or
                not isinstance(state.get('seq'), int) or isinstance(state.get('seq'), bool) or
                not observed < state['seq'] <= maximum or
                not isinstance(state.get('hash'), str) or len(state['hash']) != 64 or
                not isinstance(state.get('shards'), list) or len(state['shards']) != 256):
            raise Fault('Shared checkpoint identity mismatch', 503)
        if state['seq'] == hint['seq'] and (state['hash'] != hint['hash'] or result['digest'] != hint['digest']):
            raise Fault('Shared checkpoint manifest mismatch', 503)
        lease = result.get('lease')
        staged = CheckpointStage(state['seq'], state['hash'], directory)
        try:
            for bucket, digest in enumerate(state['shards']):
                if digest is None:
                    continue
                for prefix, record in self._checkpoint_records(digest, '%02x' % bucket, lease):
                    if (not isinstance(record, list) or len(record) != 3 or
                            not isinstance(record[0], str) or not isinstance(record[1], str) or
                            not isinstance(record[2], list) or not record[2]):
                        raise Fault('Invalid shared checkpoint record', 503)
                    kind, key, chunks = record
                    # Gateway uses JSON.stringify for key hashing, not the
                    # native canonical encoder (which escapes Unicode).
                    identity = json.dumps([kind, key], ensure_ascii=False, separators=(',', ':'))
                    if not hashlib.sha256(identity.encode('utf-8')).hexdigest().startswith(prefix):
                        raise Fault('Shared checkpoint shard placement mismatch', 503)
                    parts, size = [], 0
                    for start in range(0, len(chunks), 8):
                        for part in self._checkpoint_objects(chunks[start:start + 8], lease):
                            text = self._checkpoint_bytes(part, 262144)
                            size += len(text.encode('utf-8'))
                            if size > 4194304:
                                raise Fault('Shared checkpoint record exceeds bound', 503)
                            parts.append(text)
                    raw = ''.join(parts)
                    value = json.loads(raw)
                    if value is None:
                        raise Fault('Invalid shared checkpoint tombstone', 503)
                    staged.add(kind, key, raw)
            staged.finish()
            if lease:
                staged.release_callback = lambda: self.request('checkpoint/release', {'lease': lease})
                staged.renew_callback = lambda: self.request('checkpoint/renew', {'lease': lease})
            return staged
        except BaseException:
            staged.close()
            if lease:
                try:
                    self.request('checkpoint/release', {'lease': lease})
                except Fault:
                    pass
            raise

    def _checkpoint_records(self, digest, prefix, lease):
        pending = [(digest, prefix)]
        while pending:
            digest, prefix = pending.pop()
            shard = self._checkpoint_objects([digest], lease)[0]
            node = json.loads(self._checkpoint_bytes(shard, 524288))
            if isinstance(node, list):
                for record in node:
                    yield prefix, record
            elif (isinstance(node, dict) and node.get('format') == 'branches' and
                  isinstance(node.get('children'), list) and len(node['children']) == 16 and len(prefix) < 64):
                for index, child in enumerate(node['children']):
                    if child is not None:
                        pending.append((child, prefix + '%x' % index))
            else:
                raise Fault('Invalid shared checkpoint shard', 503)

    def _checkpoint_bytes(self, result, maximum):
        encoded = result.get('encoded')
        if (not isinstance(encoded, str) or len(encoded.encode('utf-8')) > maximum or
                hashlib.sha256(encoded.encode('utf-8')).hexdigest() != result.get('digest')):
            raise Fault('Shared checkpoint integrity or size failure', 503)
        return encoded

    def _checkpoint_objects(self, digests, lease=None):
        if any(not isinstance(d, str) or len(d) != 64 or any(c not in '0123456789abcdef' for c in d) for d in digests):
            raise Fault('Invalid shared checkpoint object digest', 503)
        result = self.request('checkpoint/objects', {'digests': digests, 'lease': lease})
        objects = result.get('objects')
        if not isinstance(objects, list) or [r.get('digest') for r in objects] != digests:
            raise Fault('Shared checkpoint object page mismatch', 503)
        return objects

    def append(self, expected, entry):
        return self.request('append', {'expected': int(expected), 'entry': encode(entry)})

    def status(self):
        return self.request('status', {})


class CheckpointStage(dict):
    """Disk-backed verified records; network staging never holds mirror lock."""
    def __init__(self, seq, digest, directory=None):
        super(CheckpointStage, self).__init__(seq=seq, hash=digest)
        self.directory = tempfile.TemporaryDirectory(prefix='biomerte-checkpoint-', dir=directory)
        self.path = os.path.join(self.directory.name, 'records.sqlite')
        self.db = sqlite3.connect(self.path)
        self.db.execute('CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')

    def add(self, kind, key, raw):
        try:
            self.db.execute('INSERT INTO records VALUES(?,?,?)', (kind, key, raw))
        except sqlite3.IntegrityError:
            raise Fault('Duplicate shared checkpoint record', 503)

    def finish(self):
        self.db.commit()

    def records(self):
        for kind, key, raw in self.db.execute('SELECT kind,id,value FROM records ORDER BY kind,id'):
            yield kind, key, json.loads(raw)

    def renew(self):
        callback = getattr(self, 'renew_callback', None)
        if callback:
            callback()

    def release(self):
        callback = getattr(self, 'release_callback', None)
        self.release_callback = None
        self.renew_callback = None
        if callback:
            try:
                callback()
            except Fault:
                pass  # expiry releases an uncertain close safely

    def close(self):
        self.release()
        if getattr(self, 'db', None) is not None:
            self.db.close()
            self.db = None
            self.directory.cleanup()

    def __del__(self):
        self.close()


class JournalReplay:
    """Bounded-memory fetched suffix, independent of the live mirror writer."""
    def __init__(self, directory=None):
        self.directory = tempfile.TemporaryDirectory(prefix='biomerte-replay-', dir=directory)
        self.db = sqlite3.connect(os.path.join(self.directory.name, 'tail.sqlite'))
        self.db.execute('CREATE TABLE entries(seq INTEGER PRIMARY KEY,value TEXT)')

    def __setitem__(self, seq, value):
        self.db.execute('INSERT INTO entries VALUES(?,?)', (seq, encode(value)))

    def __getitem__(self, seq):
        row = self.db.execute('SELECT value FROM entries WHERE seq=?', (seq,)).fetchone()
        return json.loads(row[0]) if row else None

    def __del__(self):
        if getattr(self, 'db', None) is not None:
            self.db.close()
            self.directory.cleanup()
