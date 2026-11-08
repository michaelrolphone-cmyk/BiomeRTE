import hashlib
import json
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
import io
from biomerte.store import Store, Fault, encode, digest
from biomerte.gateway_journal import GatewayJournal


class CheckpointTests(unittest.TestCase):
    def remote(self, corrupt=False):
        objects = {}
        shards = [None] * 256
        for kind, key, raw in [('runs', 'r', '{"state":"running","n":9007199254740993,"f":1.0}'),
                               ('_receipt_stream', '0', '{"sequence":5}')]:
            value_hash = hashlib.sha256(raw.encode()).hexdigest()
            objects[value_hash] = raw
            identity = json.dumps([kind, key], ensure_ascii=False, separators=(',', ':'))
            bucket = int(hashlib.sha256(identity.encode()).hexdigest()[:2], 16)
            shard = json.dumps([[kind, key, [value_hash]]])
            shard_hash = hashlib.sha256(shard.encode()).hexdigest()
            objects[shard_hash] = shard
            if kind == 'runs':
                children = [None] * 16
                children[int(hashlib.sha256(identity.encode()).hexdigest()[2], 16)] = shard_hash
                branch = json.dumps({'format': 'branches', 'children': children})
                branch_hash = hashlib.sha256(branch.encode()).hexdigest()
                objects[branch_hash] = branch
                shard_hash = branch_hash
            shards[bucket] = shard_hash
        state = {'format': 2, 'site': 's', 'seq': 7, 'hash': 'a' * 64, 'shards': shards}
        tail = {'seq': 8, 'prev': state['hash'], 'changes': [['runs', 'r', {'state': 'done', 'n': 9007199254740993}]]}
        head = {'seq': 8, 'hash': digest(tail)}
        encoded = json.dumps(state)
        manifest = {'seq': 7, 'hash': state['hash'], 'digest': hashlib.sha256(encoded.encode()).hexdigest()}
        remote = GatewayJournal({'origin': 'https://example.test', 'key_file': 'unused'}, 's')
        def request(path, payload):
            if path == 'read':
                if payload['key'] == 'head':
                    return {'value': head, 'version': '8', 'checkpoint': manifest}
                self.assertEqual(payload['key'], 'log/00000000000000000008')
                return {'encoded': encode(dict(tail, prev='bad') if corrupt else tail)}
            if path == 'checkpoint/objects':
                return {'objects': [{'digest': d, 'encoded': objects[d]} for d in payload['digests']]}
            self.assertEqual(path, 'checkpoint')
            return {'encoded': encoded, 'digest': manifest['digest']}
        remote.request = request
        return remote, state

    def test_empty_replica_restore_tail_and_receipts(self):
        remote, _ = self.remote()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 's', remote)
            self.assertEqual(store.execute(lambda tx: tx.need('runs', 'r'), read_only=True),
                             {'state': 'done', 'n': 9007199254740993})
            self.assertEqual(store.execute(lambda tx: tx.need('_receipt_stream', '0'), cached=True), {'sequence': 5})
            db = store.connect()
            self.assertEqual(db.execute('SELECT seq FROM position').fetchone()[0], 8)
            self.assertEqual(db.execute("SELECT value FROM record_lookup WHERE kind='runs' AND field='state'").fetchone()[0], encode('done'))
            db.close()

    def test_failed_tail_rolls_back_entire_restore(self):
        remote, _ = self.remote(True)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 's')
            store.execute(lambda tx: tx.put('old', 'x', {'safe': True}))
            store.remote = remote
            with self.assertRaises(Fault):
                store.execute(lambda tx: tx.rows('runs'), read_only=True)
            self.assertEqual(store.execute(lambda tx: tx.need('old', 'x'), cached=True), {'safe': True})
            db = store.connect()
            self.assertEqual(db.execute('SELECT seq FROM position').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='runs'").fetchone()[0], 0)
            db.close()

    def test_restore_removes_stale_records(self):
        remote, _ = self.remote()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 's')
            store.execute(lambda tx: tx.put('old', 'x', True))
            store.remote = remote
            self.assertEqual(store.execute(lambda tx: tx.rows('old'), read_only=True), [])

    def test_corrupt_digest_site_and_duplicates_rejected(self):
        remote, state = self.remote()
        remote.read('head')
        for mutation in ('digest', 'site', 'shards'):
            value = dict(state)
            if mutation == 'site': value['site'] = 'other'
            if mutation == 'shards': value['shards'] = state['shards'] * 2
            encoded = json.dumps(value)
            result = {'encoded': encoded, 'digest': hashlib.sha256(encoded.encode()).hexdigest()}
            remote.checkpoint_hint = dict(seq=7, hash=state['hash'], digest=result['digest'])
            if mutation == 'digest': result['digest'] = 'bad'
            remote.request = lambda path, payload: result
            with self.assertRaises(Fault): remote.checkpoint(0, 8)

    def test_read_lease_covers_snapshot_and_tail_fetch_then_releases(self):
        remote, _ = self.remote()
        original = remote.request
        calls = []
        def request(path, payload):
            if path in ('checkpoint/renew', 'checkpoint/release'):
                self.assertEqual(payload['lease'], 'reader-token')
                calls.append(path)
                return {'renewed': True}
            result = original(path, payload)
            if path == 'checkpoint':
                result['lease'] = 'reader-token'
            if path == 'checkpoint/objects':
                self.assertEqual(payload['lease'], 'reader-token')
            return result
        remote.request = request
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 's', remote)
            store.execute(lambda tx: tx.need('runs', 'r'), read_only=True)
        self.assertEqual(calls, ['checkpoint/renew', 'checkpoint/release'])

    def test_append413_preserves_status_and_rolls_back_local_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = directory + '/key'
            with open(key_file, 'w') as handle:
                handle.write('test-only')
            remote = GatewayJournal({'origin': 'https://example.test', 'key_file': key_file}, 's')
            opener = mock.Mock()
            opener.open.side_effect = HTTPError('https://example.test', 413, 'Too large', {}, io.BytesIO(b''))
            with mock.patch('biomerte.gateway_journal.build_opener', return_value=opener):
                with self.assertRaises(Fault) as caught:
                    remote.append('0', {'seq': 1, 'prev': '', 'changes': []})
            self.assertEqual(caught.exception.status, 413)
            store = Store(directory, 's')
            store.execute(lambda tx: tx.put('safe', 'x', True))
            class RejectingJournal:
                def read(self, name):
                    return None, '0'
                def append(self, expected, entry):
                    raise Fault('Transaction exceeds configured size limit', 413)
            store.remote = RejectingJournal()
            with self.assertRaises(Fault) as caught:
                store.execute(lambda tx: tx.put('safe', 'x', False))
            self.assertEqual(caught.exception.status, 413)
            self.assertTrue(store.execute(lambda tx: tx.get('safe', 'x'), cached=True))

    def test_object_pages_follow_bounded_cursor_and_validate_each_prefix(self):
        remote, _ = self.remote()
        values = ['"one"', '"two"', '"three"']
        objects = {hashlib.sha256(v.encode()).hexdigest(): v for v in values}
        requested = list(objects)
        calls = []
        def request(path, payload):
            self.assertEqual(path, 'checkpoint/objects')
            calls.append(payload['digests'])
            digest = payload['digests'][0]
            return {'objects': [{'digest': digest, 'encoded': objects[digest]}],
                    'next': 1 if len(payload['digests']) > 1 else None}
        remote.request = request
        result = list(remote._checkpoint_object_pages(requested))
        self.assertEqual(len(calls), 3)
        self.assertEqual([part['digest'] for page in result for part in page], requested)
        remote.request = lambda path, payload: {'objects': [{'digest': requested[0], 'encoded': objects[requested[0]]}], 'next': None}
        with self.assertRaises(Fault):
            list(remote._checkpoint_object_pages(requested))

    def test_no_new_checkpoint_uses_legacy_replay(self):
        remote, _ = self.remote()
        self.assertIsNone(remote.checkpoint(0, 8))
        remote.read('head')
        self.assertIsNone(remote.checkpoint(7, 8))


if __name__ == '__main__': unittest.main()

class DescriptorBatchTests(unittest.TestCase):
    def test_roots_batch_with_duplicate_digests_and_paginated_prefixes(self):
        remote = GatewayJournal({'origin': 'https://example.test', 'key_file': 'unused'}, 's')
        empty = '[]'
        hashed = hashlib.sha256(empty.encode()).hexdigest()
        calls = []
        def request(path, payload):
            self.assertEqual(path, 'checkpoint/objects')
            self.assertEqual(payload['lease'], 'pin')
            calls.append(payload['digests'])
            count = min(3, len(payload['digests']))
            return {'objects': [{'digest': d, 'encoded': empty}
                                for d in payload['digests'][:count]],
                    'next': count if count < len(payload['digests']) else None}
        remote.request = request
        roots = [(hashed, '%02x' % i) for i in range(256)]
        result = list(remote._checkpoint_leaf_batches(roots, 'pin', 128))
        self.assertEqual(sorted(prefix for prefix, _ in result),
                         sorted(prefix for _, prefix in roots))
        self.assertEqual(len(calls), 86)
        self.assertTrue(all(len(c) <= 128 for c in calls))

    def test_batched_descriptor_corruption_still_fails_hash_validation(self):
        remote = GatewayJournal({'origin': 'https://example.test', 'key_file': 'unused'}, 's')
        digest = hashlib.sha256(b'[]').hexdigest()
        remote.request = lambda path, payload: {
            'objects': [{'digest': digest, 'encoded': '[1]'}], 'next': None}
        with self.assertRaises(Fault):
            list(remote._checkpoint_leaf_batches([(digest, '00')], None, 128))
