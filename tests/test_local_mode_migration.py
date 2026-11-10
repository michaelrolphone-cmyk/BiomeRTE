import copy
import json
import os
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from biomerte.store import Store, encode


def load():
    path = os.path.join(os.path.dirname(__file__), '..', 'deploy', 'local_mode_migration.py')
    # module_from_spec was added after the supported native Python 3.4 runtime.
    module = types.ModuleType('local_mode_migration')
    module.__file__ = path
    with open(path, 'rb') as handle:
        exec(compile(handle.read(), path, 'exec'), module.__dict__)
    return module


class LocalModeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # macOS commonly exposes TMPDIR through /var -> /private/var. The
        # staging contract deliberately requires a canonical private path.
        self.root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.root, 'controller')
        os.mkdir(self.home)
        self.config = {'site': 'site1', 'node': 'controller-a', 'incarnation': 'original',
                       'admin_token': 'never-print-this', 'public_key': 'public-key',
                       'gateway_journal': {'controller_id': 'transport-a', 'origin': 'https://invalid.test'}}
        self.write_config()
        for name in ('node.key', 'local.sqlite', 'worker.sqlite', 'events.sqlite'):
            with open(os.path.join(self.home, name), 'wb') as handle:
                handle.write(('DO NOT TOUCH ' + name).encode())
        self.store = Store(self.home, 'gateway-site1')
        self.records = {
            ('_meta', 'initialized'): 1,
            ('_meta', 'events'): 9,
            ('contracts', 'test.execute:1'): {'id': 'test.execute:1', 'inputs': {}, 'outputs': {}},
            ('peers', 'provider-a'): {'id': 'provider-a', 'key': 'PUBLIC A', 'enabled': True, 'scopes': []},
            ('peers', 'provider-b'): {'id': 'provider-b', 'key': 'PUBLIC B', 'enabled': True, 'scopes': []},
            ('_enforcers', 'resource-a'): 'provider-a',
            ('_enforcers', 'resource-b'): 'provider-b',
            ('offers', 'offer-a'): {'id': 'offer-a', 'provider': 'provider-a', 'resource': 'resource-a',
                                   'contract': 'test.execute:1', 'state': 'ready', 'configuration': {}, 'service': ''},
            ('offers', 'offer-b'): {'id': 'offer-b', 'provider': 'provider-b', 'resource': 'resource-b',
                                   'contract': 'test.execute:1', 'state': 'ready', 'configuration': {}, 'service': ''},
            ('guards', 'resource-a'): {'holder': 'lease-a', 'generation': 17},
            ('guards', 'resource-b'): {'holder': 'lease-b', 'generation': 27},
            ('leases', 'lease-a'): {'id': 'lease-a', 'provider': 'provider-a', 'resource': 'resource-a',
                'guard': 'resource-a', 'generation': 17, 'contract': 'test.execute:1', 'state': 'bound',
                'offer': 'offer-a', 'task': 'task-a', 'requested_resource': 'resource-a'},
            ('leases', 'lease-b'): {'id': 'lease-b', 'provider': 'provider-b', 'resource': 'resource-b',
                'guard': 'resource-b', 'generation': 27, 'contract': 'test.execute:1', 'state': 'bound',
                'offer': 'offer-b', 'task': 'task-b', 'requested_resource': 'resource-b'},
            ('actions', 'action-a'): {'id': 'action-a', 'provider': 'provider-a', 'resource': 'resource-a',
                'lease': 'lease-a', 'guard': 'resource-a', 'generation': 17, 'contract': 'test.execute:1',
                'state': 'uncertain', 'inputs': {'important': 'keep exact'}, 'result': {'quiescent': False}},
            ('actions', 'action-b'): {'id': 'action-b', 'provider': 'provider-b', 'resource': 'resource-b',
                'lease': 'lease-b', 'guard': 'resource-b', 'generation': 27, 'contract': 'test.execute:1',
                'state': 'claimed', 'inputs': {'command': 'never run twice'}},
            ('work', 'task-a'): {'id': 'task-a', 'state': 'open', 'kind': 'task', 'parent': '',
                               'dependencies': [], 'attempts': []},
            ('work', 'task-b'): {'id': 'task-b', 'state': 'running', 'kind': 'task', 'parent': '',
                               'dependencies': [], 'attempts': ['run-b']},
            ('process_versions', 'version1'): {'id': 'version1', 'graph': {'start': 'end', 'nodes': {
                'end': {'type': 'end', 'result': {}}}}, 'calls': {}, 'name': 'example'},
            ('definitions', 'example'): {'id': 'example', 'rev': 'version1'},
            ('runs', 'run-b'): {'id': 'run-b', 'state': 'running', 'task': 'task-b', 'rev': 'version1',
                'name': 'example', 'parent': '', 'children': [], 'tokens': [], 'steps': 1, 'trace': []},
            ('subscriptions', 'subscription-b'): {'id': 'subscription-b', 'process': 'example',
                                                   'topic': 'user.start', 'cursor': 9},
            ('documents', 'note'): {'key': 'note', 'heads': ['revision1']},
            ('revisions', 'revision1'): {'id': 'revision1', 'body': {'text': 'durable knowledge'}},
            ('_retention', 'actions:action-a'): {'kind': 'actions', 'key': 'action-a', 'born': 1},
        }
        self.persist()
        self.manifest = self.make_manifest()

    def write_config(self):
        with open(os.path.join(self.home, 'node.json'), 'w') as handle:
            json.dump(self.config, handle)

    def persist(self):
        self.store.execute(lambda tx: [tx.put(kind, key, value) for (kind, key), value in self.records.items()])
        with self.store.connect() as db:
            db.execute("UPDATE position SET seq=777,hash='original-head' WHERE id=1")

    def make_manifest(self):
        assignments = {}
        for kind, key in self.records:
            owner = 'shared' if kind in self.module.SHARED_KINDS or kind == '_meta' else 'controller-b' if key.endswith('-b') else 'controller-a'
            assignments.setdefault(kind, {})[key] = owner
        return {'format': 1, 'site': 'site1', 'inventory_sha256': self.module.inventory(self.home)['sha256'],
            'controllers': {'controller-a': {'transport': 'transport-a', 'evidence': 'verified/a'},
                            'controller-b': {'transport': 'transport-b', 'evidence': 'verified/b'}},
            'providers': {'provider-' + tag: {'owner': 'controller-' + tag, 'evidence': 'verified/provider-' + tag,
                'authentication': {'mode': 'native_peer', 'actor': 'provider-' + tag, 'realm': 'site',
                                   'evidence': 'verified/native-provider-auth-' + tag}} for tag in 'ab'},
            'resources': {'resource-' + tag: {'owner': 'controller-' + tag, 'provider': 'provider-' + tag,
                           'evidence': 'verified/resource-' + tag} for tag in 'ab'},
            'records': assignments,
            'qualifications': {key: 'qualification-fixture/' + key for key in self.module.QUALIFICATIONS}}

    def plan(self, manifest=None):
        return self.module.plan(self.home, manifest or self.manifest)

    def assert_blocked(self, phrase, manifest=None):
        result = self.plan(manifest)
        self.assertFalse(result['ready_to_stage'])
        self.assertIn(phrase, '\n'.join(result['blockers']))
        return result

    def output(self, name='candidate'):
        path = os.path.join(self.root, name)
        os.mkdir(path, 0o700)
        return os.path.join(path, 'site.sqlite')

    def test_inventory_is_deterministic_bounded_and_omits_values(self):
        first = self.module.inventory(self.home)
        self.assertEqual(first, self.module.inventory(self.home))
        self.assertEqual(first['position'], [777, 'original-head'])
        self.assertNotIn('never-print-this', encode(first))
        self.assertNotIn('keep exact', encode(first))
        for limits in ({'rows': 1}, {'bytes': 1}, {'row_bytes': 1}):
            with self.assertRaisesRegex(ValueError, 'bounds'):
                self.module.inventory(self.home, limits)

    def retention_manifest(self):
        from biomerte.retention_selection import plan
        self.records['_retention', 'actions:action-a']['id'] = 'actions:action-a'
        self.records['events', '00000000000000000001'] = {'seq': 1, 'topic': 'old', 'data': {}}
        self.records['_retention', 'events:00000000000000000001'] = {
            'id': 'events:00000000000000000001', 'kind': 'events',
            'key': '00000000000000000001', 'born': 0, 'terminal_at': 0}
        self.persist()
        self.manifest = self.make_manifest()
        with self.module.snapshot(self.home) as inputs:
            selected = plan(inputs[2], 100, seconds=10, keep=1024, historical=False)
        self.manifest['retention_selection'] = selected
        for item in selected['omitted']:
            self.manifest['records'][item['kind']].pop(item['id'], None)
        return selected

    def test_stage_excludes_exact_eligible_history_without_touching_source(self):
        selected = self.retention_manifest()
        self.assertIn(('events', '00000000000000000001'), {(x['kind'], x['id']) for x in selected['omitted']})
        before = self.module.inventory(self.home)
        plan = self.plan()
        self.assertEqual(plan['blockers'], [])
        path = self.output()
        self.module.stage(self.home, self.manifest, path, plan['sha256'])
        with sqlite3.connect(path) as db:
            self.assertIsNone(db.execute("SELECT value FROM records WHERE kind='events'").fetchone())
            meta = json.loads(db.execute("SELECT value FROM records WHERE kind='_meta' AND id='operational_retention'").fetchone()[0])
            self.assertEqual(meta['event_floor'], 1)
            self.assertIsNotNone(db.execute("SELECT value FROM records WHERE kind='actions' AND id='action-a'").fetchone())
        self.assertEqual(self.module.inventory(self.home), before)

    def test_untrusted_omission_is_recomputed_before_staging(self):
        selected = self.retention_manifest()
        selected['omitted'].append({'kind': 'actions', 'id': 'action-a',
                                    'sha256': self.module.sha(self.records['actions', 'action-a']), 'reason': 'forged'})
        with self.assertRaisesRegex(ValueError, 'unverified omissions'):
            self.plan()

    def test_changed_retention_preimage_invalidates_reviewed_plan(self):
        self.retention_manifest()
        before = self.plan()
        self.records['events', '00000000000000000001']['data'] = {'changed': True}
        self.persist()
        with self.assertRaisesRegex(ValueError, 'selection changed'):
            self.module.stage(self.home, self.manifest, self.output(), before['sha256'])

    def test_repair_retirement_preserves_live_guard_authority_in_candidate(self):
        from biomerte.retention_selection import plan as select
        self.retention_manifest()
        self.records['_operator_purges', 'repair'] = {
            'id': 'repair', 'realm': 'site', 'completed_at': '2020-01-01T00:00:00+00:00',
            'counts': {'guards': 2}, 'journal_mode': 'one_primary_record_per_transaction',
            'native_mac_quiescent': False, 'remote_native_quiescence': 'unresolved actions preserved'}
        self.records['guards', 'resource-a']['operator_purge'] = 'repair'
        self.records['guards', 'resource-b']['operator_purge'] = 'repair'
        self.persist()
        self.manifest = self.make_manifest()
        review = {'schema': 'completed_repair_v1', 'installed_reader_evidence': 'fixture/no-readers',
                  'canonical_schema_evidence': 'fixture/schemas'}
        with self.module.snapshot(self.home) as inputs:
            selected = select(inputs[2], 2000000000, historical=False, repair_review=review)
        self.manifest['retention_selection'] = selected
        for row in selected['omitted']:
            self.manifest['records'][row['kind']].pop(row['id'], None)
        plan = self.plan()
        self.assertEqual(plan['blockers'], [])
        path = self.output()
        self.module.stage(self.home, self.manifest, path, plan['sha256'])
        with sqlite3.connect(path) as db:
            guard = json.loads(db.execute("SELECT value FROM records WHERE kind='guards' AND id='resource-a'").fetchone()[0])
            self.assertEqual(guard, {'holder': 'lease-a', 'generation': 17})
            self.assertIsNone(db.execute("SELECT value FROM records WHERE kind='_operator_purges'").fetchone())
        self.assertEqual(self.records['guards', 'resource-a']['operator_purge'], 'repair')

    def test_partition_keeps_exact_unresolved_state_and_only_current_tables(self):
        before = {}
        for name in ('node.json', 'node.key', 'local.sqlite', 'worker.sqlite', 'events.sqlite'):
            with open(os.path.join(self.home, name), 'rb') as handle:
                before[name] = handle.read()
        plan = self.plan()
        self.assertEqual(plan['blockers'], [])
        output = self.output()
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network permitted')):
            result = self.module.stage(self.home, self.manifest, output, plan['sha256'])
        self.assertFalse(result['activation_performed'])
        with sqlite3.connect(output) as db:
            actual = {(k, i): json.loads(v) for k, i, v in db.execute('SELECT * FROM records')}
            self.assertEqual(db.execute('SELECT * FROM position').fetchall(), [(1, 0, '')])
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, {'records', 'position'})
        for ref in (('actions', 'action-a'), ('leases', 'lease-a'), ('guards', 'resource-a'),
                    ('work', 'task-a'), ('documents', 'note'), ('revisions', 'revision1'), ('_retention', 'actions:action-a')):
            self.assertEqual(actual[ref], self.records[ref])
        for ref in (('actions', 'action-b'), ('leases', 'lease-b'), ('runs', 'run-b'),
                    ('subscriptions', 'subscription-b'), ('work', 'task-b'), ('peers', 'provider-b')):
            self.assertNotIn(ref, actual)
        self.assertEqual(actual['offers', 'offer-a']['state'], 'ready')
        self.assertEqual(actual['offers', 'offer-b']['state'], 'offline')
        for name, original in before.items():
            with open(os.path.join(self.home, name), 'rb') as handle:
                self.assertEqual(handle.read(), original)
        self.assertEqual(self.module.inventory(self.home)['sha256'], self.manifest['inventory_sha256'])

    def test_second_owner_has_exclusive_live_execution(self):
        a_output = self.output('a')
        self.module.stage(self.home, self.manifest, a_output, self.plan()['sha256'])
        self.config['node'] = 'controller-b'
        self.write_config()
        b_output = self.output('b')
        self.module.stage(self.home, self.manifest, b_output, self.plan()['sha256'])
        for output, mine, foreign in ((a_output, 'a', 'b'), (b_output, 'b', 'a')):
            store = Store(os.path.dirname(output), 'site')
            from biomerte.providers import pending
            from biomerte.api import Context
            inbox = store.execute(lambda tx: pending(tx, Context(None), 'provider-' + foreign), read_only=True)
            self.assertEqual(inbox, {'fences': [], 'actions': [], 'activations': [], 'streams': []})
            self.assertEqual(store.execute(lambda tx: tx.need('offers', 'offer-' + foreign), read_only=True)['state'], 'offline')
            self.assertEqual(store.execute(lambda tx: tx.need('actions', 'action-' + mine), read_only=True), self.records['actions', 'action-' + mine])

    def test_requires_every_mapping_and_qualification(self):
        m = copy.deepcopy(self.manifest)
        del m['records']['actions']['action-a']
        self.assert_blocked('Missing single record owner', m)
        m = copy.deepcopy(self.manifest)
        m['qualifications'] = {}
        p = self.assert_blocked('Native qualification required', m)
        with self.assertRaisesRegex(ValueError, 'not qualified'):
            self.module.stage(self.home, m, self.output(), p['sha256'])

    def test_rejects_ambiguous_provider_and_transport_mapping(self):
        m = copy.deepcopy(self.manifest)
        m['providers']['provider-a']['owner'] = 'controller-b'
        self.assert_blocked('Resource/provider owner mismatch', m)
        m = copy.deepcopy(self.manifest)
        m['controllers']['controller-b']['transport'] = 'transport-a'
        self.assert_blocked('transport mapping is ambiguous', m)
        m = copy.deepcopy(self.manifest)
        del m['providers']['provider-a']
        self.assert_blocked('Unverified provider/resource/action owner', m)

    def test_authentication_path_is_explicit_not_inferred_from_provider_id(self):
        m = copy.deepcopy(self.manifest)
        del m['providers']['provider-a']['authentication']
        self.assert_blocked('authentication path is unqualified', m)
        m = copy.deepcopy(self.manifest)
        m['providers']['provider-a']['authentication']['actor'] = 'provider-b'
        self.assert_blocked('does not satisfy provider.owner', m)

    def test_existing_gateway_oauth_provider_needs_no_invented_native_peer(self):
        del self.records['peers', 'provider-a']
        self.store.execute(lambda tx: tx.drop('peers', 'provider-a'))
        self.manifest = self.make_manifest()
        self.manifest['providers']['provider-a']['authentication'] = {
            'mode': 'gateway_oauth', 'realm': 'site', 'endpoint': 'https://gateway.invalid/api/mcp',
            'evidence': 'verified/existing-gateway-auth-and-owner-route'}
        p = self.plan()
        self.assertEqual(p['blockers'], [])
        output = self.output()
        self.module.stage(self.home, self.manifest, output, p['sha256'])
        with sqlite3.connect(output) as db:
            self.assertIsNone(db.execute("SELECT value FROM records WHERE kind='peers' AND id='provider-a'").fetchone())
            self.assertEqual(json.loads(db.execute("SELECT value FROM records WHERE kind='actions' AND id='action-a'").fetchone()[0]), self.records['actions', 'action-a'])

    def test_gateway_auth_descriptor_cannot_embed_credentials_or_change_realm(self):
        m = copy.deepcopy(self.manifest)
        m['providers']['provider-a']['authentication'] = {
            'mode': 'gateway_oauth', 'realm': 'site', 'endpoint': 'https://user:secret@gateway.invalid/api/mcp',
            'evidence': 'verified/existing-path'}
        self.assert_blocked('endpoint is unqualified', m)
        m['providers']['provider-a']['authentication']['endpoint'] = 'https://gateway.invalid/api/mcp'
        m['providers']['provider-a']['authentication']['realm'] = 'local'
        self.assert_blocked('authentication path is unqualified', m)

    def test_no_shared_work_runs_actions_subscriptions_or_credentials(self):
        for kind, key in (('work', 'task-b'), ('runs', 'run-b'), ('actions', 'action-b'),
                          ('subscriptions', 'subscription-b'), ('peers', 'provider-b')):
            m = copy.deepcopy(self.manifest)
            m['records'][kind][key] = 'shared'
            self.assert_blocked('cannot be shared', m)

    def test_future_selection_and_grants_have_one_owner(self):
        self.records['grants', 'grant-a'] = {'id': 'grant-a', 'task': 'task-a',
            'state': 'issued', 'rev': 'version1', 'to': 'provider-a'}
        self.persist()
        self.manifest = self.make_manifest()
        m = copy.deepcopy(self.manifest)
        m['records']['grants']['grant-a'] = 'shared'
        self.assert_blocked('cannot be shared', m)
        from biomerte.work import ready
        selections, grants = [], []
        for owner in ('controller-a', 'controller-b'):
            self.config['node'] = owner
            self.write_config()
            output = self.output(owner)
            self.module.stage(self.home, self.manifest, output, self.plan()['sha256'])
            store = Store(os.path.dirname(output), 'site')
            selections.append(store.execute(lambda tx: [w['id'] for w in tx.rows('work') if ready(tx, w)]))
            grants.append(store.execute(lambda tx: [g['id'] for g in tx.rows('grants')]))
        self.assertEqual(selections, [['task-a'], []])
        self.assertEqual(grants, [['grant-a'], []])

    def test_shared_retention_marker_accompanies_shared_payload(self):
        self.records['artifacts', 'artifact1'] = {'id': 'artifact1', 'data': 'payload'}
        self.records['_retention', 'artifacts:artifact1'] = {'kind': 'artifacts', 'key': 'artifact1', 'terminal_at': 1}
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('Retention eligibility must accompany every copied record')
        self.manifest['records']['_retention']['artifacts:artifact1'] = 'shared'
        self.assertEqual(self.plan()['blockers'], [])
        output = self.output()
        self.module.stage(self.home, self.manifest, output, self.plan()['sha256'])
        with sqlite3.connect(output) as db:
            self.assertIsNotNone(db.execute("SELECT value FROM records WHERE kind='_retention' AND id='artifacts:artifact1'").fetchone())

    def test_events_are_singular_and_pending_delivery_cannot_be_split(self):
        self.records['events', '00000000000000000010'] = {'id': '00000000000000000010', 'seq': 10, 'topic': 'user.start', 'payload': {}}
        self.records['_retention', 'events:00000000000000000010'] = {'kind': 'events', 'key': '00000000000000000010', 'terminal_at': 1}
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('Cross-owner dependency subscriptions/subscription-b -> events/')
        self.manifest['records']['events']['00000000000000000010'] = 'shared'
        self.assert_blocked('cannot be shared')
        self.manifest['records']['events']['00000000000000000010'] = 'controller-b'
        self.manifest['records']['_retention']['events:00000000000000000010'] = 'controller-b'
        self.assertEqual(self.plan()['blockers'], [])
        output = self.output()
        self.module.stage(self.home, self.manifest, output, self.plan()['sha256'])
        with sqlite3.connect(output) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='events'").fetchone()[0], 0)

    def test_resource_admission_policy_is_not_silently_lost(self):
        self.records['mission', 'current'] = {'allocation': {'terminal': None}}
        self.records['offers', 'offer-a']['service'] = 'terminal'
        self.records['_authorization_protected', 'protection-a'] = {'contract': 'test.execute:1', 'resource': 'resource-a'}
        self.persist()
        self.manifest = self.make_manifest()
        self.assertEqual(self.plan()['blockers'], [])
        m = copy.deepcopy(self.manifest)
        m['records']['mission']['current'] = 'shared'
        self.assert_blocked('cannot be shared', m)
        m = copy.deepcopy(self.manifest)
        m['records']['mission']['current'] = 'controller-b'
        self.assert_blocked('Mission allocation spans owners', m)
        m = copy.deepcopy(self.manifest)
        m['records']['_authorization_protected']['protection-a'] = 'controller-b'
        self.assert_blocked('Authorization protection must remain', m)

    def test_dependency_stored_in_record_key_cannot_split(self):
        self.records['_submission_active', 'action-a'] = {'id': 'action-a'}
        self.records['_submissions', 'action-a'] = {'id': 'action-a', 'lease': 'lease-a'}
        self.persist()
        self.manifest = self.make_manifest()
        self.manifest['records']['_submission_active']['action-a'] = 'controller-b'
        self.assert_blocked('Cross-owner dependency _submission_active/action-a -> _submissions/action-a')

    def test_no_nested_archive_or_coordination_state_is_admitted(self):
        for kind in ('_coordination', '_outbox', 'journal_snapshot'):
            self.records[kind, 'old'] = {'state': 'active'}
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('Unqualified record kind')

    def test_mission_cannot_smuggle_executable_work(self):
        self.records['mission', 'current'] = {'allocation': {}, 'tasks': [{'state': 'ready'}]}
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('not the qualified inert allocation schema')

    def test_retirement_inventory_never_deletes_and_tracks_sidecars_and_caches(self):
        for name in ('cluster-site1.sqlite', 'cluster-site1.sqlite-wal', 'cluster-site1.sqlite-shm'):
            with open(os.path.join(self.home, name), 'wb') as handle:
                handle.write(b'old')
        cache = os.path.join(self.home, 'biomerte-checkpoint-example')
        os.mkdir(cache)
        p = self.plan()['post_adoption_retirement']
        names = {item['path'] for item in p['items']}
        self.assertIn('gateway-site1.sqlite', names)
        self.assertIn('cluster-site1.sqlite-wal', names)
        self.assertIn('cluster-site1.sqlite-shm', names)
        self.assertIn('biomerte-checkpoint-example', names)
        self.assertFalse(p['deletion_performed'])
        self.assertFalse(p['recovery_copy_created'])
        self.assertIn('expiry deadline', p['disposal_lifecycle'])
        self.assertTrue(os.path.isdir(cache))

    def test_staging_rejects_public_or_symlink_directory(self):
        output = self.output()
        os.chmod(os.path.dirname(output), 0o755)
        with self.assertRaisesRegex(ValueError, 'private'):
            self.module.stage(self.home, self.manifest, output, self.plan()['sha256'])
        os.chmod(os.path.dirname(output), 0o700)
        link = os.path.join(self.root, 'link')
        os.symlink(os.path.dirname(output), link)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.module.stage(self.home, self.manifest, os.path.join(link, 'site.sqlite'), self.plan()['sha256'])

    def test_cleanup_never_unlinks_replaced_path(self):
        output = self.output()
        def replace_and_fail(fd):
            os.unlink(output)
            with open(output, 'w') as handle:
                handle.write('replacement must survive')
            raise OSError('replacement')
        with patch.object(self.module.os, 'fsync', side_effect=replace_and_fail):
            with self.assertRaises(OSError):
                self.module.stage(self.home, self.manifest, output, self.plan()['sha256'])
        with open(output) as handle:
            self.assertEqual(handle.read(), 'replacement must survive')

    def test_released_guard_generation_stays_with_resource(self):
        self.records['guards', '!slot:resource-a:3'] = {'holder': '', 'generation': 99}
        self.persist()
        self.manifest = self.make_manifest()
        self.manifest['records']['guards']['!slot:resource-a:3'] = 'controller-b'
        self.assert_blocked('Guard generation would leave its resource owner')

    def test_snapshot_is_lazy_and_one_consistent_wal_read(self):
        with self.module.snapshot(self.home) as (_, _, records, summary):
            self.assertIsInstance(records, self.module.RecordSnapshot)
            original = records['work', 'task-a']
            self.store.execute(lambda tx: tx.put('work', 'task-a', dict(original, state='accepted')))
            self.assertEqual(records['work', 'task-a'], original)
        self.assertNotEqual(self.module.inventory(self.home)['sha256'], summary['sha256'])

    def test_candidate_disk_bound_aborts_without_partial_file(self):
        for i in range(120):
            self.records['artifacts', 'filler-%03d' % i] = {'data': 'x' * 50}
        self.persist()
        self.manifest = self.make_manifest()
        limits = {'bytes': 20000}
        p = self.module.plan(self.home, self.manifest, limits)
        self.assertEqual(p['blockers'], [])
        output = self.output()
        with self.assertRaisesRegex(sqlite3.OperationalError, 'full'):
            self.module.stage(self.home, self.manifest, output, p['sha256'], limits)
        self.assertFalse(os.path.exists(output))

    def test_rejects_cross_owner_dependency_and_broken_fence(self):
        self.records['runs', 'run-b']['tokens'] = [{'action': 'action-a'}]
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('Cross-owner dependency runs/run-b -> actions/action-a')
        self.records['guards', 'resource-a']['generation'] = 18
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('Live lease fence mismatch')

    def test_rejects_missing_lease_and_unknown_extensions(self):
        self.store.execute(lambda tx: tx.drop('leases', 'lease-a'))
        del self.records['leases', 'lease-a']
        self.manifest = self.make_manifest()
        self.assert_blocked('Missing dependency leases/lease-a')
        self.records['new_scheduler_kind', 'item'] = {'state': 'active'}
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('Unqualified record kind')

    def test_rejects_active_dynamic_and_foreign_routes(self):
        version = self.records['process_versions', 'version1']
        version['graph']['nodes']['service'] = {'type': 'service', 'contract': {'$': 'input.contract'}}
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('Active dynamic capability route')
        version['graph']['nodes']['service'] = {'type': 'service', 'contract': 'test.execute:1', 'resource': 'resource-a'}
        self.persist()
        self.manifest = self.make_manifest()
        self.assert_blocked('foreign/unverified capability')

    def test_candidate_cannot_overwrite_or_change_home(self):
        p = self.plan()
        with self.assertRaisesRegex(ValueError, 'outside controller home'):
            self.module.stage(self.home, self.manifest, os.path.join(self.home, 'site.sqlite'), p['sha256'])
        output = self.output()
        with open(output, 'w') as handle:
            handle.write('existing')
        with self.assertRaises(FileExistsError):
            self.module.stage(self.home, self.manifest, output, p['sha256'])
        with open(output) as handle:
            self.assertEqual(handle.read(), 'existing')

    def test_changes_since_plan_fail_before_creating_candidate(self):
        p = self.plan()
        self.records['actions', 'action-a']['state'] = 'succeeded'
        self.persist()
        output = self.output()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.module.stage(self.home, self.manifest, output, p['sha256'])
        self.assertFalse(os.path.exists(output))

    def test_failure_removes_incomplete_candidate(self):
        p = self.plan()
        output = self.output()
        with patch.object(self.module.os, 'fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.module.stage(self.home, self.manifest, output, p['sha256'])
        self.assertFalse(os.path.exists(output))

    def test_source_selection_refuses_old_standalone_or_mixed_config(self):
        with self.assertRaisesRegex(ValueError, 'Already configured'):
            self.module.source_path(self.home, dict(self.config, gateway_link={'controller_id': 'a'}))
        with self.assertRaisesRegex(ValueError, 'Mixed'):
            self.module.source_path(self.home, dict(self.config, etcd={'endpoints': ['local']}))
        with self.assertRaisesRegex(ValueError, 'No shared mirror'):
            self.module.source_path(self.home, {'site': 'site1'})
        conf = dict(self.config)
        del conf['gateway_journal']
        conf['etcd'] = {'endpoints': ['local']}
        self.assertTrue(self.module.source_path(self.home, conf).endswith('cluster-site1.sqlite'))

    def test_committed_wal_is_included(self):
        before = self.module.inventory(self.home)['sha256']
        db = self.store.connect()
        self.addCleanup(db.close)
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('INSERT INTO records VALUES(?,?,?)', ('work', 'wal-only', encode({'state': 'open'})))
        after = self.module.inventory(self.home)
        self.assertNotEqual(before, after['sha256'])
        self.assertTrue(any(r['id'] == 'wal-only' for r in after['records']))


if __name__ == '__main__':
    unittest.main()
