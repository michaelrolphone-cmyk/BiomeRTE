import json
import os
import shutil
import sys
import threading
import time
import unittest
from biomerte.app import Application
from biomerte.store import Fault, uid, encode, b64
from biomerte.worker import Worker
from biomerte.package_install import install
from tests import test_runtime as runtime
ROOT = runtime.ROOT


class LocalRemote:
    def __init__(self, app, node):
        self.app, self.node, self.site, self.realm = app, node, app.site, 'site'
        self.drop = False

    def call(self, name, args):
        if name == 'provider.result' and self.drop:
            self.drop = False
            raise IOError('simulated link failure after external effect, before delivery')
        return self.app.call(name, args, uid())


class Operations(unittest.TestCase):
    setUp = runtime.Runtime.setUp
    call = runtime.Runtime.call
    drain = runtime.Runtime.drain
    peer = runtime.Runtime.peer
    trust = runtime.Runtime.trust

    def test_adapter_restart_does_not_repeat_effect(self):
        effect = os.path.join(self.tmp.name, 'effects.txt')
        code = "import sys,json; r=json.load(sys.stdin); open(sys.argv[1],'a').write(r['operation']+'\\n'); print(json.dumps({'value':r['inputs']['value']}))"
        self.call('cap.define', {'name': 'device.write', 'inputs': {
                  'value': 'str'}, 'outputs': {'value': 'str'}})
        remote = LocalRemote(self.a, 'board')
        offers = [{'contract': 'device.write:1', 'resource': 'pin',
                   'argv': [sys.executable, '-c', code, effect]}]
        path = os.path.join(self.tmp.name, 'ledger.sqlite')
        w = Worker(path, remote, offers)
        w.advertise()
        l = self.call('cap.acquire', {'contract': 'device.write:1', 'task': 't'})
        self.call('cap.invoke', {'lease': l['id'],
                  'operation': 'set-a', 'inputs': {'value': 'green'}})
        w.once()
        remote.drop = True
        w.once()
        self.assertEqual(w.last_health['state'], 'degraded')
        self.assertEqual(w.last_health['blocked'][0]['id'], 'set-a')
        self.assertIn('simulated link failure', w.last_health['blocked'][0]['reason'])
        w.db.close()
        w = Worker(path, remote, offers)
        w.once()
        w.db.close()
        self.assertEqual(self.call('cap.status', {'operation': 'set-a'})['state'], 'succeeded')
        with open(effect) as f:
            self.assertEqual(f.readlines(), ['set-a\n'])

    def test_survey_input_branches_and_state(self):
        self.call('cap.define', {'name': 'radio.measure', 'inputs': {
                  'frequency': 'num'}, 'outputs': {'dbfs': 'num'}})
        self.call('cap.define', {'name': 'radio.capture', 'inputs': {
                  'frequency': 'num'}, 'outputs': {'reference': 'str'}})
        for cap in ('measure', 'capture'):
            self.call('cap.offer', {'provider': 'radio',
                      'contract': 'radio.' + cap + ':1', 'resource': 'demo.' + cap})
        for path in ('measure.json', 'branching-survey.json'):
            with open(os.path.join(ROOT, 'examples', path)) as f:
                self.call('routine.publish', json.load(f))
        for threshold, expected in [(-50, 'capture-A'), (-40, None)]:
            r = self.call('process.start', {'name': 'survey.band', 'inputs': {'frequencies': [101, 102],
                                                                              'threshold': threshold, 'capture': True}})
            for unused in range(90):
                self.a.tick('site')
                pending = self.call('provider.poll', {'provider': 'radio'})
                for l in pending['fences']:
                    self.call('cap.fenced', {'provider': 'radio',
                              'lease': l['id'], 'generation': l['generation']})
                for a in pending['actions']:
                    args = {'provider': 'radio',
                            'operation': a['id'], 'generation': a['generation']}
                    self.call('provider.claim', args)
                    out = {'dbfs': -47 if a['inputs']['frequency'] == 102 else - \
                           60} if a['contract'] == 'radio.measure:1' else {'reference': 'capture-A'}
                    self.call('provider.result', dict(args, state='succeeded', result=out))
                r = self.call('process.get', {'run': r['id']})
                if r['state'] != 'running':
                    break
            self.assertEqual(r['result'], {'frequency': 102,
                             'peak': -47, 'capture': expected, 'errors': []})

    def test_subscription_local_delay_and_bus_inbox(self):
        self.call('routine.publish', {'name': 'on.event', 'inputs': {'event': 'obj'}, 'outputs': {'name': 'str'},
                  'steps': [{'type': 'timer', 'seconds': 0.01}], 'result': {'name': {'$': 'input.event.topic'}}})
        self.call('bus.subscribe', {'name': 'response',
                  'topic': 'user.test', 'process': 'on.event'})
        self.call('bus.publish', {'topic': 'user.test', 'payload': {}})
        self.a.tick('site')
        time.sleep(.04)
        self.drain()
        self.assertEqual([r['state'] for r in self.call('process.list')['runs']], ['completed'])
        self.call('bus.send', {'to': 'operator', 'body': {'note': 'done'}})
        msg = self.call('bus.inbox', {'address': 'operator'})[0]
        self.assertTrue(self.call('bus.ack_message', {'message': msg['id']})['read'])

    def test_generated_adapter_probe_and_activation(self):
        src = b"import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'doubled':2*r['inputs']['x']}))\n"
        c = self.call('cap.define', {'name': 'math.double', 'inputs': {
                      'x': 'int'}, 'outputs': {'doubled': 'int'}})
        ref = self.call('artifact.put', {'data': b64(src)})
        p = self.call('package.stage', {'name': 'double', 'contract': c['id'], 'artifact': ref['sha256'],
                                        'probes': [{'inputs': {'x': 3}, 'expected': {'doubled': 6}}]})
        manifest = os.path.join(self.tmp.name, 'worker.json')
        with open(manifest, 'w') as f:
            json.dump({'offers': []}, f)
        install(self.a, p['id'], 'cpu', manifest)
        with open(manifest) as f:
            cfg = json.load(f)
        w = Worker(os.path.join(self.tmp.name, 'w.sqlite'),
                   LocalRemote(self.a, 'host'), cfg['offers'])
        w.advertise()
        l = self.call('cap.acquire', {'contract': c['id'], 'task': 'job'})
        self.call('cap.invoke', {'lease': l['id'], 'operation': 'double-21', 'inputs': {'x': 21}})
        w.once()
        w.once()
        w.db.close()
        self.assertEqual(
            self.call('cap.status', {'operation': 'double-21'})['result'], {'doubled': 42})

    def test_explicit_migration_and_parallel_conflict(self):
        g = {'nodes': {'start': {'type': 'end', 'result': {'n': 1}}},
             'start': 'start', 'outputs': {'n': 'int'}}
        first = self.call('process.publish', {'name': 'v', 'graph': g})
        r = self.call('process.start', {'name': 'v', 'inputs': {}})
        g['nodes']['start']['result']['n'] = 2
        second = self.call('process.publish', {'name': 'v', 'graph': g})
        self.call('process.migrate', {
                  'run': r['id'], 'expected_rev': first['rev'], 'target_rev': second['rev'], 'nodes': {'start': 'start'}})
        self.drain()
        self.assertEqual(self.call('process.get', {'run': r['id']})['result'], {'n': 2})
        g = {'start': 'fork', 'nodes': {'fork': {'type': 'fork', 'branches': ['a', 'b']},
                                        'a': {'type': 'set', 'values': {'x': 1}, 'next': 'j'}, 'b': {'type': 'set', 'values': {'x': 2}, 'next': 'j'},
                                        'j': {'type': 'join', 'next': 'e'}, 'e': {'type': 'end'}}}
        self.call('process.publish', {'name': 'conflict', 'graph': g})
        r = self.call('process.start', {'name': 'conflict', 'inputs': {}})
        self.drain()
        self.assertEqual(self.call('process.get', {'run': r['id']})['state'], 'failed')

    def test_untrusted_package_cannot_redirect_to_existing_local_definition(self):
        from biomerte.identity import signed
        b = self.peer()
        self.trust(b, self.a, ['delegate'], 'local')
        defn = self.call('routine.publish', {'name': 'private', 'steps': [], 'inputs': {
        }, 'outputs': {}, 'result': {}}, app=b, realm='local')
        payload = {'format': 1, 'site': self.a.site, 'sender': self.a.node, 'to': b.node, 'kind': 'grant', 'data': {
            'id': 'malicious', 'task': 't', 'versions': [], 'contracts': [], 'rev': defn['rev'], 'name': 'private', 'inputs': {}}}
        with self.assertRaises(Fault):
            self.call('offline.accept', {'packet': signed(
                self.a.key, payload)}, realm='local', app=b)

    def test_malformed_physical_result_stays_uncertain(self):
        self.call('cap.define', {'name': 'motor.run',
                  'inputs': {}, 'outputs': {'observed': 'bool'}})
        cfg = [{'contract': 'motor.run:1', 'resource': 'motor',
                'argv': [sys.executable, '-c', 'print("{}")']}]
        w = Worker(os.path.join(self.tmp.name, 'invalid.sqlite'),
                   LocalRemote(self.a, 'driver'), cfg)
        w.advertise()
        l = self.call('cap.acquire', {'task': 'test', 'contract': 'motor.run:1'})
        self.call('cap.invoke', {'operation': 'effect', 'lease': l['id'], 'inputs': {}})
        w.once()
        w.once()
        w.db.close()
        self.assertEqual(self.call('cap.status', {'operation': 'effect'})['state'], 'uncertain')

    def test_configurable_offer_fails_once_then_reoffers(self):
        self.call('cap.define', {'name': 'test.mode', 'inputs': {}, 'outputs': {}})
        offer = {'provider': 'device', 'contract': 'test.mode:1',
                 'resource': 'radio', 'state': 'available_after_setup'}
        self.call('cap.offer', offer)
        req = self.call('cap.prepare', {'contract': 'test.mode:1'})
        self.call('cap.configured', {
                  'provider': 'device', 'activation': req['id'], 'success': False, 'evidence': {'probe': 'failed'}})
        self.assertEqual(self.call('cap.prepare', {'contract': 'test.mode:1'})['state'], 'unmet')
        self.call('cap.offer', offer)
        req = self.call('cap.prepare', {'contract': 'test.mode:1'})
        self.call('cap.configured', {
                  'provider': 'device', 'activation': req['id'], 'success': True, 'evidence': {'probe': 'ready'}})
        self.assertEqual(self.call('cap.acquire', {'task': 'job', 'contract': 'test.mode:1'})[
                         'state'], 'fencing')

    def test_compatible_offline_plan_edits(self):
        b = self.peer()
        self.trust(self.a, b, ['sync'])
        self.trust(b, self.a, ['sync'])
        self.call('work.create', {'kind': 'task', 'id': 'survey', 'title': 'Survey', 'priority': 1})

        def pull(dst, src):
            c = self.call('sync.cursors', app=dst)
            p = self.call('sync.export', {'to': dst.node, 'cursors': c}, app=src)
            self.call('sync.import', {'packet': p}, app=dst)
        pull(b, self.a)

        def apply(app):
            heads = self.call('knowledge.get', {'key': 'plan.survey'}, app=app)['heads']
            return self.call('plan.apply', {'id': 'survey', 'parents': heads, 'values': {}}, app=app)
        btask = apply(b)
        self.call('work.update', {'id': 'survey', 'revision': 1, 'changes': {'priority': 3}})
        self.call('work.update', {'id': 'survey', 'revision': btask['revision'], 'changes': {
                  'title': 'Field survey'}}, app=b)
        pull(self.a, b)
        heads = self.call('knowledge.get', {'key': 'plan.survey'})['heads']
        self.assertEqual(len(heads), 2)
        self.call('knowledge.merge', {'key': 'plan.survey', 'heads': heads})
        r = apply(self.a)
        self.assertEqual((r['priority'], r['title']), (3, 'Field survey'))

    def test_missing_capability_is_actionable_and_wait_resumes(self):
        self.call('routine.publish', {'name': 'needs-construction', 'inputs': {}, 'outputs': {},
                  'steps': [{'type': 'service', 'contract': 'absent:1', 'args': {}}], 'result': {}})
        run = self.call('process.start', {'name': 'needs-construction', 'inputs': {}})
        self.drain()
        tasks = self.call('work.list', {'kind': 'task'})
        self.assertEqual([t['title'] for t in tasks], ['Supply absent:1'])
        self.assertIn(tasks[0]['id'], self.call('process.get', {
                      'run': run['id']})['tokens'][0]['blocked'])
        self.call('cap.define', {'name': 'absent', 'inputs': {}, 'outputs': {}})
        self.call('cap.offer', {'provider': 'new', 'contract': 'absent:1', 'resource': 'r'})
        self.drain()
        self.assertEqual(len(self.call('provider.poll', {'provider': 'new'})['fences']), 1)
        self.assertEqual(len(self.call('work.list', {'kind': 'task'})), 1)

    def test_nested_failure_returns_other_results(self):
        self.call('routine.publish', {'name': 'bad', 'inputs': {}, 'outputs': {}, 'steps': [
                  {'type': 'fail', 'reason': 'sample failed'}], 'result': {}})
        args = {'name': 'survey', 'inputs': {}, 'outputs': {'errors': 'list', 'done': 'bool'}, 'steps': [
            {'type': 'set', 'values': {'errors': []}},
            {'each': [1, 2], 'as': 'frequency', 'do': [{'try': [{'type': 'call', 'process': 'bad'}],
                                                        'catch': [{'type': 'set', 'values': {'errors': {'op': 'add', 'args': [{'$': 'errors'}, [{'$': 'frequency'}]]}}}]}]}],
            'result': {'errors': {'$': 'errors'}, 'done': True}}
        self.call('routine.publish', args)
        run = self.call('process.start', {'name': 'survey', 'inputs': {}})
        self.drain()
        self.assertEqual(self.call('process.get', {'run': run['id']})[
                         'result'], {'errors': [1, 2], 'done': True})
