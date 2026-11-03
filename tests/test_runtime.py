import contextlib
import copy
import json
import os
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from biomerte.app import Application, initialize
from biomerte.api import Context
from biomerte.knowledge import put
from biomerte.store import Fault, uid, digest
from biomerte.identity import sign
from biomerte.worker import Worker
from biomerte.web import server
from biomerte.xmlprofile import compile_xml
from biomerte.backup import backup, restore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'a')
        initialize(self.home)
        self.a = Application(self.home)

    def call(self, n, a=None, rid=None, realm='site', app=None):
        return (app or self.a).call(n, a or {}, rid or uid(), realm)

    def drain(self, app=None, realm='site'):
        for i in range(300):
            if not (app or self.a).tick(realm):
                return
        self.fail('runner failed to settle')

    def peer(self):
        h = os.path.join(self.tmp.name, 'b')
        initialize(h)
        return Application(h)

    def trust(self, who, other, scopes, realm='site'):
        self.call('peer.trust', {
                  'public_key': other.config['public_key'], 'label': 'test peer', 'scopes': scopes}, realm=realm, app=who)

    def test_selective_registry_deletion(self):
        cid = self.call('cap.define', {'name': 'sample', 'inputs': {}, 'outputs': {}})['id']
        args = {'provider': 'sample', 'resource': 'slot', 'contract': cid}
        oid = self.call('cap.offer', args)['id']
        lease = self.call('cap.acquire', {'task': 'test', 'contract': cid})
        delete_contract = {'contract': cid, 'delete_contract': True}
        with self.assertRaises(Fault):
            self.call('cap.delete', {'offer': oid})
        with self.assertRaises(Fault):
            self.call('cap.delete', delete_contract)
        self.call('cap.release', {'lease': lease['id']})
        receipt = self.call('cap.delete', {'offer': oid}, 'delete-once')
        self.assertEqual(receipt, self.call('cap.delete', {'offer': oid}, 'delete-once'))
        self.assertTrue(receipt['audit_event'])
        self.assertEqual([], self.call('cap.discover', {'contract': cid})['offers'])
        self.call('cap.delete', delete_contract)
        self.assertEqual([], self.call('cap.discover', {'contract': cid})['contracts'])
        with self.assertRaises(Fault):
            self.call('cap.delete', {'contract': 'core.echo:1', 'delete_contract': True})

    def test_demo_restart_and_accept(self):
        ids = self.call('demo.run', {'message': 'first'})
        self.a.tick('site')
        self.a = Application(self.home)
        self.drain()
        r = self.call('process.get', {'run': ids['run']})
        self.assertEqual(r['state'], 'completed')
        rev = r['result']['revision']
        self.assertEqual(self.call('knowledge.get', {
                         'key': ids['document'], 'rev': rev})['body'], 'first')
        task = next(i for i in self.call('work.list') if i['id'] == ids['task'])
        self.assertEqual(task['state'], 'review')
        with self.assertRaises(Fault):
            self.call('work.accept', {'id': task['id'],
                      'revision': task['revision'], 'evidence': {}})
        self.assertEqual(self.call('work.accept', {'id': task['id'], 'revision': task['revision'],
                         'evidence': {'stored result': rev}})['state'], 'accepted')
        self.assertTrue(all(l['state'] == 'released' for l in self.call(
            'cap.leases', {'task': task['id']})))

    def test_idempotence_and_readiness(self):
        args = {'title': 'Ready later', 'kind': 'task', 'id': 'a'}
        self.assertEqual(self.call('work.create', args, 'repeat'),
                         self.call('work.create', args, 'repeat'))
        with self.assertRaises(Fault):
            self.call('work.create', dict(args, title='different'), 'repeat')
        self.call('work.create', {'title': 'Most urgent', 'kind': 'task',
                  'id': 'b', 'priority': 9, 'dependencies': ['a']})
        rows = self.call('work.list')
        self.assertEqual([x['id'] for x in rows if x['ready']], ['a'])
        with self.assertRaises(Fault):
            self.call('work.update', {'id': 'a', 'revision': 1, 'changes': {'dependencies': ['b']}})
        self.call('work.update', {'id': 'a', 'revision': 1, 'changes': {'sequence': 3}})
        with self.assertRaises(Fault):
            self.call('work.update', {'id': 'a', 'revision': 1, 'changes': {'sequence': 1}})

    def test_parallel_join_call_versions(self):
        child = {'name': 'child', 'steps': [{'type': 'set', 'values': {'v': 'original'}}],
                 'inputs': {}, 'outputs': {'value': 'str'}, 'result': {'value': {'$': 'v'}}}
        self.call('routine.publish', child)
        graph = {'inputs': {}, 'outputs': {'v': 'str', 'b': 'int'}, 'start': 'f', 'nodes': {
            'f': {'type': 'fork', 'branches': ['left', 'right']},
            'left': {'type': 'call', 'process': 'child', 'args': {}, 'save': 'left', 'next': 'join'},
            'right': {'type': 'set', 'values': {'b': 2}, 'next': 'join'}, 'join': {'type': 'join', 'next': 'done'},
            'done': {'type': 'end', 'result': {'v': {'$': 'left.value'}, 'b': {'$': 'b'}}}}}
        self.call('process.publish', {'name': 'parent', 'graph': graph})
        self.call('routine.publish', dict(child, steps=[
                  {'type': 'set', 'values': {'v': 'changed'}}]))
        r = self.call('process.start', {'name': 'parent', 'inputs': {}})
        self.drain()
        self.assertEqual(self.call('process.get', {'run': r['id']})[
                         'result'], {'v': 'original', 'b': 2})

    def test_human_bpmn_and_mcp(self):
        with open(os.path.join(ROOT, 'examples', 'review.bpmn')) as f:
            xml = f.read()
        self.call('bpmn.import', {'name': 'review', 'xml': xml})
        r = self.call('process.start', {'name': 'review', 'inputs': {'item': 'A'}})
        self.drain()
        w = self.call('process.waits', {'run': r['id']})[0]
        self.call('process.answer', {'wait': w['id'], 'answer': {'accepted': True}})
        with self.assertRaises(Fault):
            self.call('process.answer', {'wait': w['id'], 'answer': {'accepted': False}})
        self.drain()
        self.assertEqual(self.call('process.get', {'run': r['id']})['result'], {'accepted': True})
        with self.assertRaises(Fault):
            compile_xml(xml.replace('userTask', 'scriptTask'))
        from biomerte.mcp import respond
        out = respond(self.a, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'invoke',
                                                                                             'arguments': {'operation': 'system.info', 'arguments': {}}}}, True)
        self.assertEqual(json.loads(out['result']['content'][0]['text'])['site'], self.a.site)

    def test_hardware_receipts_and_fences(self):
        c = self.call('cap.define', {'name': 'meter.read', 'inputs': {}, 'outputs': {'v': 'int'}})
        self.call('cap.offer', {'provider': 'board', 'contract': c['id'], 'resource': 'sensor'})
        l = self.call('cap.acquire', {'task': 'sample', 'contract': c['id']})
        a = self.call('cap.invoke', {'lease': l['id'], 'inputs': {}, 'operation': 'op-one'})
        self.assertEqual(self.call('provider.poll', {'provider': 'board'})['actions'], [])
        self.call('cap.fenced', {'provider': 'board', 'lease': l['id'], 'generation': 1})
        args = {'provider': 'board', 'operation': a['id'], 'generation': 1}
        self.call('provider.claim', args)
        self.call('provider.result', dict(args, state='uncertain', result={'reason': 'lost reply'}))
        with self.assertRaises(Fault):
            self.call('cap.release', {'lease': l['id']})
        self.call('provider.result', dict(args, state='succeeded', result={'v': 7}))
        self.assertEqual(self.call('provider.result', dict(
            args, state='succeeded', result={'v': 7}))['result'], {'v': 7})
        self.call('cap.release', {'lease': l['id']})
        l2 = self.call('cap.acquire', {'task': 'other', 'contract': c['id']})
        self.assertEqual(l2['generation'], 2)
        with self.assertRaises(Fault):
            self.call('cap.fenced', {'provider': 'board', 'lease': l['id'], 'generation': 1})

    def test_routine_exposure_learning(self):
        r = self.call('demo.run')
        self.drain()
        trace = self.call('process.get', {'run': r['run']})['trace']
        self.call('routine.learn', {'run': r['run'], 'name': 'learned', 'bindings': {
                  trace[0]['activity']: {'value': 'new_value'}}})
        offer = self.call('routine.expose', {'name': 'learned',
                          'capability': 'learn.echo', 'version': 1})
        l = self.call('cap.acquire', {'contract': offer['contract'], 'task': 'different'})
        a = self.call('cap.invoke', {'lease': l['id'], 'inputs': {
                      'new_value': 'new input'}, 'operation': 'run-learned'})
        self.drain()
        a = self.call('cap.status', {'operation': a['id']})
        self.assertEqual(a['result']['last']['value'], 'new input')

    def test_mission_pressure(self):
        ss = [{'id': 'alarm', 'priority': 3, 'needs': ['receiver'], 'modes': [{'name': 'on', 'cost': {'power': 3}}]},
              {'id': 'receiver', 'priority': 1, 'modes': [{'name': 'on', 'cost': {'power': 1}}]},
              {'id': 'survey', 'priority': 2, 'modes': [
                  {'name': 'low', 'cost': {'power': 2}}, {'name': 'full', 'cost': {'power': 6}}]},
              {'id': 'upload', 'priority': 0, 'modes': [{'name': 'on', 'cost': {'power': 2}}]}]
        for n, expected in [(12, ('on', 'full', 'on')), (6, ('on', 'low', None)), (3, (None, 'low', None))]:
            a = self.call('mission.apply', {'budget': {'power': n}, 'services': ss})['allocation']
            self.assertEqual(tuple(a[k] for k in ('alarm', 'survey', 'upload')), expected)

    def test_offline_signed_conflicts_and_duplicate_ranges(self):
        b = self.peer()
        self.trust(self.a, b, ['sync'])
        self.trust(b, self.a, ['sync'])
        base = self.call('knowledge.put', {'key': 'field', 'body': 'base'})

        def pull(dst, src):
            curs = self.call('sync.cursors', app=dst)
            pack = self.call('sync.export', {'to': dst.node, 'cursors': curs}, app=src)
            return pack, self.call('sync.import', {'packet': pack}, app=dst)
        pull(b, self.a)
        self.call('knowledge.put', {'key': 'field', 'body': 'east', 'parents': [base['rev']]})
        self.call('knowledge.put', {'key': 'field',
                  'body': 'west', 'parents': [base['rev']]}, app=b)
        p, ack = pull(self.a, b)
        self.assertEqual(ack['applied'], 1)
        self.assertEqual(self.call('sync.import', {'packet': p})['applied'], 0)
        d = self.call('knowledge.get', {'key': 'field'})
        self.assertTrue(d['conflict'])
        self.assertEqual(len(d['heads']), 2)
        self.call('knowledge.put', {'key': 'field', 'body': 'resolved', 'parents': d['heads']})
        pull(b, self.a)
        self.assertFalse(self.call('knowledge.get', {'key': 'field'}, app=b)['conflict'])
        corrupt = copy.deepcopy(p)
        corrupt['payload']['data']['records'][0]['body'] = 'tampered'
        with self.assertRaises(Fault):
            self.call('sync.import', {'packet': corrupt})

    def test_scoped_offline_delegation(self):
        b = self.peer()
        self.trust(self.a, b, ['return'])
        self.trust(b, self.a, ['delegate', 'delegate:core.echo:1'], 'local')
        self.call('routine.publish', {'name': 'field', 'steps': [{'type': 'service', 'contract': 'core.echo:1',
                                                                  'args': {'value': {'$': 'input.v'}}, 'save': 'out'}], 'inputs': {'v': 'str'}, 'outputs': {'value': 'any'}, 'result': {'$': 'out'}})
        item = self.call('work.create', {'kind': 'task', 'title': 'Field observation'})
        args = {'task': item['id'], 'process': 'field', 'inputs': {
            'v': 'local reading'}, 'to': b.node, 'contracts': ['core.echo:1']}
        pack = self.call('offline.delegate', args)
        with self.assertRaises(Fault):
            self.call('offline.delegate', args)
        rec = self.call('offline.accept', {'packet': pack}, realm='local', app=b)
        self.assertEqual(self.call('offline.accept', {
                         'packet': pack}, realm='local', app=b)['run'], rec['run'])
        self.drain(b, 'local')
        packet = self.call('offline.result', {'incoming': rec['id']}, realm='local', app=b)
        result = self.call('offline.reconcile', {'packet': packet})
        self.assertEqual(result['outcome']['result'], {'value': 'local reading'})
        self.assertEqual(self.call('offline.reconcile', {'packet': packet}), result)

    def test_commission_proof_revocation_and_replay(self):
        b = self.peer()
        challenge = self.a.begin_pair(b.config['public_key'], 'meter')
        proof = dict(challenge, signature=sign(b.key, challenge))
        with self.assertRaises(Fault):
            self.a.finish_pair(proof)
        self.call('pair.approve', {'fingerprint': b.node, 'scopes': ['system.info']})
        self.assertEqual(self.a.finish_pair(proof)['credential'], b.node)
        args = {'operation': 'system.info', 'arguments': {},
                'site': self.a.site, 'id': 'read', 'realm': 'site'}
        auth = {'actor': b.node, 'signature': sign(b.key, args)}
        self.assertEqual(self.a.call('system.info', {}, 'read', 'site', auth)['node'], self.a.node)
        with self.assertRaises(Fault):
            self.a.call('work.create', {'title': 'attack', 'kind': 'task'}, 'read', 'site', auth)
        self.call('peer.revoke', {'peer': b.node})
        with self.assertRaises(Fault):
            self.a.finish_pair(proof)

    def test_http_and_backup_restore(self):
        srv = server(self.a, '127.0.0.1', 0)
        th = threading.Thread(target=srv.serve_forever)
        th.daemon = True
        th.start()
        try:
            url = 'http://127.0.0.1:%d' % srv.server_port
            with urlopen(url) as r:
                self.assertIn(b'Controller token', r.read())
            body = json.dumps({'operation': 'system.info', 'arguments': {}}).encode()
            req = Request(url + '/v1/call', body, {'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as err:
                urlopen(req)
            self.assertEqual(err.exception.code, 401)
            req.add_header('Authorization', 'Bearer ' + self.a.config['admin_token'])
            with urlopen(req) as response:
                self.assertEqual(json.loads(response.read().decode())['result']['realm'], 'site')
        finally:
            srv.shutdown()
            srv.server_close()
            th.join()
        self.call('knowledge.put', {'key': 'd', 'body': 'saved'})
        archive = os.path.join(self.tmp.name, 'archive')
        new = os.path.join(self.tmp.name, 'new')
        backup(self.a, archive)
        restore(archive, new)
        self.assertTrue(os.path.isfile(os.path.join(new, 'RECOVERY_HOLD')))
        with self.assertRaises(Fault):
            restore(archive, new)
