"""Authenticated workflow views read canonical state without changing it."""
import copy
import json
import os
import tempfile
import unittest

from biomerte.app import Application, initialize
from biomerte.controller_authority import ControllerAuthority
from biomerte.identity import generate, fingerprint, sign
from biomerte.store import Fault, uid

BINDING = dict(service_id='site-controller', manifest_revision='view-test',
               worker_id='mac', worker_incarnation='mac-1', boot_epoch='epoch-1',
               grant_token='grant-1')


class ProcessViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.operator_key = os.path.join(cls.keys.name, 'operator.key')
        cls.operator_public = generate(cls.operator_key)
        cls.operator_actor = fingerprint(cls.operator_public)
        cls.reader_key = os.path.join(cls.keys.name, 'reader.key')
        cls.reader_public = generate(cls.reader_key)
        cls.reader_actor = fingerprint(cls.reader_public)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.node = initialize(self.tmp.name, 'workflow-view-test')
        self.runtime = dict(authority_kind='controller', service_id=BINDING['service_id'],
                            authority_id=self.node['node'], site=self.node['site'],
                            controller_aliases=['physical-mac', 'physical-pi'],
                            operator_actors=[self.operator_actor],
                            operator_actor_aliases={self.operator_actor: 'admin'})
        self.marker('managed-authority')
        self.app = Application(self.tmp.name, controller_runtime=self.runtime)
        self.addCleanup(self.close_app)
        self.authority = ControllerAuthority(self.app, self.runtime, BINDING)
        self.app.stores['site'].execute(lambda tx: [
            tx.put('peers', self.operator_actor, dict(id=self.operator_actor, key=self.operator_public,
                   enabled=True, scopes=['controller.operator'])),
            tx.put('peers', self.reader_actor, dict(id=self.reader_actor, key=self.reader_public,
                   enabled=True, scopes=['process.definition', 'process.summaries']))])

    def close_app(self):
        for store in self.app.stores.values():
            store.close()

    def marker(self, mode):
        value = dict(format=1, mode=mode, site=self.node['site'],
                     service_id=BINDING['service_id'], authority_id=self.node['node'],
                     controller_aliases=['physical-mac', 'physical-pi'])
        if mode == 'frozen-source':
            value.update(source_node=self.node['node'], migration_id='test-migration',
                         source_digest='a' * 64)
        with open(os.path.join(self.tmp.name, 'controller-authority.json'), 'w') as handle:
            json.dump(value, handle)

    def envelope(self, operation, args=None, request=None, controller='physical-pi', reader=False):
        value = dict(site=self.node['site'], realm='site', operation=operation,
                     arguments=args or {}, id=request or uid(), controller=controller)
        actor, key = ((self.reader_actor, self.reader_key) if reader else
                      (self.operator_actor, self.operator_key))
        return dict(value, auth=dict(actor=actor, signature=sign(key, value)))

    def read(self, operation, args=None, **kwargs):
        return self.authority.call(self.envelope(operation, args, **kwargs), False)

    def publish(self, name='flow', title='Original'):
        graph = dict(start='end', nodes={'end': dict(type='end', name=title, result={})},
                     inputs={}, outputs={})
        result = self.app.call('process.publish', dict(name=name, graph=graph), uid())
        return result['rev']

    def rows(self):
        return self.app.stores['site'].execute(
            lambda tx: list(tx.db.execute('SELECT kind,id,value FROM records ORDER BY kind,id')),
            read_only=True)

    def test_latest_and_pinned_definition_preserve_exact_version(self):
        old = self.publish()
        new = self.publish(title='New revision')
        before = self.rows()
        for alias in ('physical-mac', 'physical-pi'):
            current = self.read('process.definition', dict(name='flow'), controller=alias)
            pinned = self.read('process.definition', dict(name='flow', revision=old), controller=alias)
            self.assertEqual(current['id'], new)
            self.assertEqual(pinned['id'], old)
            self.assertEqual(pinned['graph']['nodes']['end']['name'], 'Original')
            expected = self.app.stores['site'].execute(
                lambda tx: tx.need('process_versions', old), read_only=True)
            self.assertEqual(pinned, expected)
        self.assertEqual(self.rows(), before)

    def test_missing_wrong_name_and_invalid_inputs_fail_without_changes(self):
        revision = self.publish()
        before = self.rows()
        for arguments, status in [
                (dict(name='missing'), 404),
                (dict(name='other', revision=revision), 404),
                (dict(name='flow', revision='f' * 64), 404),
                (dict(name='flow', revision='bad'), 400),
                (dict(name='flow', revision=123), 400),
                (dict(name='flow', extra=True), 400)]:
            with self.subTest(arguments=arguments):
                with self.assertRaises(Fault) as raised:
                    self.read('process.definition', arguments)
                self.assertEqual(raised.exception.status, status)
        self.assertEqual(self.rows(), before)

    def test_definition_keeps_pinned_child_calls_and_large_graph_bytes(self):
        child = self.publish('child')
        graph = dict(start='call', nodes={
            'call': dict(type='call', process='child', args={}, next='end'),
            'end': dict(type='end', result={'large': 'x' * 200000})}, inputs={}, outputs={})
        revision = self.app.call('process.publish', dict(name='parent', graph=graph), uid())['rev']
        self.publish('child', 'New child')
        value = self.read('process.definition', dict(name='parent', revision=revision))
        self.assertEqual(value['graph'], graph)
        self.assertEqual(value['calls'], {'child': child})

    def test_summaries_use_each_run_pin_and_never_expose_variables_or_results(self):
        revision = self.publish()
        self.publish(title='Different current definition')
        run = dict(id='run-one', name='flow', rev=revision, state='running', steps=2, task='task-one',
                   created_at=1, started_at=2, tokens=[
                       {'at': 'end', 'vars': {'secret': 'private-variable'}, 'phase': 'waiting'},
                       {'node': 'end', 'state': 'ready'},
                       {'node_id': 'unknown', 'vars': {'secret': 'private-variable'}}],
                   result={'secret': 'private-result'}, trace=[{'secret': 'private-trace'}])
        terminal = dict(id='run-done', name='flow', rev=revision, state='completed', steps=3,
                        task='', completed_at=4, tokens=[], result={'secret': 'private-result'})
        self.app.stores['site'].execute(lambda tx: [
            tx.put('runs', run['id'], run), tx.put('runs', terminal['id'], terminal)])
        before = self.rows()
        result = self.read('process.summaries')['runs']
        current = result['run-one']['current_nodes']
        self.assertEqual(current[0], dict(id='end', name='Original', type='end', state='waiting'))
        self.assertEqual(current[1]['state'], 'ready')
        self.assertEqual(current[2], dict(id='unknown', state='active'))
        self.assertEqual(result['run-one']['started_at'], 2)
        self.assertEqual(result['run-done']['current_nodes'], [])
        self.assertEqual(result['run-done']['completed_at'], 4)
        for word in ('private-variable', 'private-result', 'private-trace', '"tokens"', '"vars"'):
            self.assertNotIn(word, json.dumps(result))
        self.assertEqual(self.rows(), before)

    def test_read_retries_do_not_write_receipts_and_do_not_return_stale_pins(self):
        first = self.publish()
        before = self.rows()
        for _ in range(2):
            self.assertEqual(self.read('process.definition', dict(name='flow'), request='same-read')['id'], first)
            self.read('process.summaries', request='same-summary')
        self.assertEqual(self.rows(), before)
        second = self.publish(title='Updated after first read')
        before = self.rows()
        self.assertEqual(self.read('process.definition', dict(name='flow'), request='same-read')['id'], second)
        self.assertEqual(self.rows(), before)

    def test_existing_auth_scopes_revocation_and_exact_alias_signature_apply(self):
        self.publish()
        self.read('process.definition', dict(name='flow'), reader=True)
        self.read('process.summaries', reader=True)
        changed = self.envelope('process.definition', dict(name='flow'))
        changed['controller'] = 'physical-mac'
        with self.assertRaises(Fault):
            self.authority.call(changed, False)
        with self.assertRaises(Fault):
            self.read('process.definition', dict(name='flow'), controller='unrelated')
        def deny(tx):
            row = tx.need('peers', self.reader_actor)
            row['scopes'] = ['system.info']
            tx.put('peers', self.reader_actor, row)
        self.app.stores['site'].execute(deny)
        with self.assertRaises(Fault) as raised:
            self.read('process.definition', dict(name='flow'), reader=True)
        self.assertEqual(raised.exception.status, 403)
        def revoke(tx):
            row = tx.need('peers', self.operator_actor)
            row['enabled'] = False
            tx.put('peers', self.operator_actor, row)
        self.app.stores['site'].execute(revoke)
        with self.assertRaises(Fault) as raised:
            self.read('process.summaries')
        self.assertEqual(raised.exception.status, 403)

    def test_frozen_source_rejects_views_while_local_realm_stays_available(self):
        self.publish()
        self.app.call('process.publish', dict(name='local-flow',
            graph=dict(start='end', nodes={'end': dict(type='end', result={})})), uid(), 'local')
        self.marker('frozen-source')
        for operation, args in [('process.definition', dict(name='flow')), ('process.summaries', {})]:
            with self.assertRaises(Fault) as raised:
                self.read(operation, args)
            self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.app.call('process.definition', dict(name='local-flow'),
                                      uid(), 'local')['name'], 'local-flow')


if __name__ == '__main__':
    unittest.main()
