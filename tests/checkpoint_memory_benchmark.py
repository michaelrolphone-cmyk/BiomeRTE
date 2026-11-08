"""Run directly: 210MB logical state, disk-backed source and native restore."""
import hashlib
import json
import os
import resource
import tempfile
from biomerte.gateway_journal import GatewayJournal
from biomerte.store import Store


def main():
    with tempfile.TemporaryDirectory() as directory:
        blobs = os.path.join(directory, 'blobs')
        os.mkdir(blobs)
        def put(text):
            digest = hashlib.sha256(text.encode()).hexdigest()
            with open(os.path.join(blobs, digest), 'w') as handle:
                handle.write(text)
            return digest
        shards = [[] for _ in range(256)]
        for i in range(600):
            kind, key = 'artifacts', 'a%d' % i
            raw = json.dumps({'id': i, 'data': ('x%03d' % i) * 87500}, separators=(',', ':'))
            chunks = [put(raw[n:n + 65536]) for n in range(0, len(raw), 65536)]
            bucket = int(hashlib.sha256(json.dumps([kind, key], separators=(',', ':')).encode()).hexdigest()[:2], 16)
            shards[bucket].append([kind, key, chunks])
        root = {'format': 2, 'site': 's', 'seq': 7, 'hash': 'a' * 64,
                'shards': [put(json.dumps(rows)) if rows else None for rows in shards]}
        encoded = json.dumps(root)
        root_digest = put(encoded)
        remote = GatewayJournal({'origin': 'https://example.test', 'key_file': 'unused'}, 's')
        maximum_page = [0]
        def request(path, payload):
            if path == 'read':
                assert payload['key'] == 'head'
                return {'value': {'seq': 7, 'hash': 'a' * 64}, 'version': '7',
                        'checkpoint': {'seq': 7, 'hash': 'a' * 64, 'digest': root_digest}}
            if path == 'checkpoint':
                return {'encoded': encoded, 'digest': root_digest}
            assert path == 'checkpoint/objects'
            objects = []
            for digest in payload['digests']:
                with open(os.path.join(blobs, digest)) as handle:
                    objects.append({'encoded': handle.read(), 'digest': digest})
            maximum_page[0] = max(maximum_page[0], sum(len(o['encoded']) for o in objects))
            return {'objects': objects}
        remote.request = request
        store = Store(os.path.join(directory, 'mirror'), 's', remote)
        store.execute(lambda tx: None, read_only=True)
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM records').fetchone()[0] == 600
            assert db.execute('SELECT seq FROM position').fetchone()[0] == 7
        assert not [p for p in os.listdir(os.path.join(directory, 'mirror')) if p.startswith('biomerte-checkpoint-')]
        print(json.dumps({'records': 600, 'payload_bytes': 210000000,
                          'maximum_object_page_chars': maximum_page[0],
                          'max_rss_kib_linux': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))


if __name__ == '__main__':
    main()
