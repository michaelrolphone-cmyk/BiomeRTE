"""77k small canonical receipts: bounded native memory and batched HTTP count."""
import hashlib
import json
import os
import resource
import sqlite3
import tempfile
import time
from biomerte.gateway_journal import GatewayJournal
from biomerte.store import Store


def main():
    with tempfile.TemporaryDirectory() as directory:
        source = sqlite3.connect(os.path.join(directory, 'source.sqlite'))
        source.executescript('CREATE TABLE objects(digest TEXT PRIMARY KEY,body TEXT);CREATE TABLE descriptors(bucket INTEGER,slot INTEGER,kind TEXT,id TEXT,digest TEXT);CREATE INDEX descriptor_route ON descriptors(bucket,slot);')
        def put(body):
            digest = hashlib.sha256(body.encode()).hexdigest()
            source.execute('INSERT OR IGNORE INTO objects VALUES(?,?)', (digest, body))
            return digest
        for i in range(77000):
            kind, key = '_receipt', 'legacy-%d' % i
            digest = put(json.dumps({'signature': 'request-%d' % i, 'result': {'ok': True, 'seq': i}}, separators=(',', ':')))
            identity = hashlib.sha256(json.dumps([kind, key], separators=(',', ':')).encode()).hexdigest()
            source.execute('INSERT INTO descriptors VALUES(?,?,?,?,?)', (int(identity[:2], 16), int(identity[2], 16), kind, key, digest))
        roots = []
        for bucket in range(256):
            children = []
            for slot in range(16):
                rows = [[kind, key, [digest]] for kind, key, digest in source.execute('SELECT kind,id,digest FROM descriptors WHERE bucket=? AND slot=?', (bucket, slot))]
                children.append(put(json.dumps(rows)) if rows else None)
            roots.append(put(json.dumps({'format': 'branches', 'children': children})))
        encoded = json.dumps({'format': 2, 'site': 's', 'seq': 77000, 'hash': 'a' * 64, 'shards': roots})
        root_digest = put(encoded)
        source.commit()
        calls, max_page = [0], [0]
        remote = GatewayJournal({'origin': 'https://example.test', 'key_file': 'unused'}, 's')
        def request(path, payload):
            if path == 'read':
                return {'value': {'seq': 77000, 'hash': 'a' * 64}, 'version': '77000', 'checkpoint': {'seq': 77000, 'hash': 'a' * 64, 'digest': root_digest}}
            if path == 'checkpoint':
                return {'encoded': encoded, 'digest': root_digest, 'object_page_limit': 128}
            assert path == 'checkpoint/objects'
            calls[0] += 1
            objects, size = [], 0
            for digest in payload['digests']:
                body = source.execute('SELECT body FROM objects WHERE digest=?', (digest,)).fetchone()[0]
                if objects and size + len(body.encode()) > 2097152:
                    break
                objects.append({'digest': digest, 'encoded': body})
                size += len(body.encode())
            max_page[0] = max(max_page[0], size)
            return {'objects': objects, 'next': len(objects) if len(objects) < len(payload['digests']) else None}
        remote.request = request
        store = Store(os.path.join(directory, 'mirror'), 's', remote)
        start = time.monotonic()
        store.execute(lambda tx: None, read_only=True)
        db = store.connect()
        assert db.execute('SELECT count(*) FROM records').fetchone()[0] == 77000
        assert db.execute('SELECT seq FROM position').fetchone()[0] == 77000
        db.close()
        assert calls[0] < 1400, calls
        print(json.dumps({'canonical_records': 77000, 'object_page_requests': calls[0], 'maximum_page_bytes': max_page[0], 'elapsed_seconds': time.monotonic() - start, 'max_rss_kib_linux': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
        source.close()


if __name__ == '__main__':
    main()
