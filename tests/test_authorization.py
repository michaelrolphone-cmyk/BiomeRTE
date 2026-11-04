import base64
import copy
import json
import os
import tempfile
import threading
import unittest
from biomerte.app import Application, initialize
from biomerte.identity import sign
from biomerte.store import Fault, uid

ROOT = os.path.dirname(os.path.dirname(__file__))


def proposal(value='permitted', revision=1, kind='single'):
    descriptor = dict(contract='core.echo:1', resource='builtin.echo', inputs={'value': value})
    return dict(revision=revision, action=descriptor, targets=['test echo'], affected_systems=['Biome test'],
                data_operations=['return input'], expected_effects=['echo the supplied value'], consequences=['test result retained'],
                scope=dict(kind=kind, **({'actions': [descriptor]} if kind != 'single' else {})), conditions=[], recovery_actions=[])


class Authorization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'controller')
        initialize(self.home)
        self.app = Application(self.home)
        phone_home = os.path.join(self.tmp.name, 'phone')
        self.phone = initialize(phone_home)
        self.phone_key = os.path.join(phone_home, 'node.key')
        pairing = self.call('decision.pair', dict(label='Acceptance phone'))
        e = dict(command='enroll', pair_id=pairing['pair_id'], code=pairing['code'],
                 public_key=self.phone['public_key'], installation='acceptance-phone')
        self.device = self.call('decision.mobile', dict(envelope=e))['device']
        with open(os.path.join(ROOT, 'examples/biome-authorization.bpmn')) as f:
            self.call('bpmn.import', dict(name='biome-authorization', xml=f.read()))

    def call(self, name, args=None, rid=None):
        return self.app.call(name, args or {}, rid or uid())

    def mobile(self, command, args, id=None):
        e = dict(device=self.device, site=self.app.site, id=id or uid(), command=command,
                 payload=base64.b64encode(json.dumps(args, ensure_ascii=False).encode()).decode())
        e['signature'] = sign(self.phone_key, e)
        return self.call('decision.mobile', dict(envelope=e))

    def drain(self):
        for _ in range(200):
            if not self.app.tick('site'):
                return
        self.fail('Workflow did not settle')

    def start(self, p=None, request='request-1', workflow='caller-1'):
        args = dict(request_id=request, workflow_instance_id=workflow, proposal=p or proposal(), question='Permit this concrete action?')
        run = self.call('process.start', dict(name='biome-authorization', inputs=args))
        self.drain()
        return run['id']

    def inbox(self):
        return self.mobile('inbox', {})['decisions']

    def respond(self, row, choice='approve', text='', id=None):
        return self.mobile('respond', dict(decision_id=row['id'], digest=row['digest'],
            proposal_revision=row['request']['proposal_revision'], choice=choice, text=text), id)

    def grant(self, p=None):
        run = self.start(p)
        row = self.inbox()[0]
        response = self.respond(row)
        self.drain()
        result = self.call('process.get', dict(run=run))['result']
        self.assertEqual('approved', result['outcome'])
        return result['authorization_id'], response

    def test_pending_restart_dedup_and_exact_trace(self):
        run = self.start()
        before = self.inbox()[0]
        self.app = Application(self.home)
        self.drain()
        after = self.inbox()
        self.assertEqual([before['id']], [x['id'] for x in after])
        first = self.respond(before, id='same-human-response')
        self.assertEqual(first, self.respond(before, id='same-human-response'))
        self.drain()
        result = self.call('process.get', dict(run=run))['result']
        dispatched = self.call('authorization.dispatch', dict(id=result['authorization_id'], proposal=proposal(), operation='exact-operation'))
        self.assertEqual('succeeded', dispatched['state'])
        self.assertEqual(dispatched, self.call('authorization.dispatch', dict(id=result['authorization_id'], proposal=proposal(), operation='exact-operation')))
        trace = self.call('authorization.get', dict(id=result['authorization_id']))
        self.assertEqual(1, len(trace['usage']))
        self.assertEqual(first['id'], trace['usage'][0]['response_id'])
        self.assertEqual(before['id'], trace['usage'][0]['decision_id'])
        self.assertEqual({'value': 'permitted'}, trace['usage'][0]['result'])
        with self.assertRaises(Fault):
            self.call('authorization.dispatch', dict(id=result['authorization_id'], proposal=proposal(), operation='another-operation'))

    def test_two_unrelated_bpmn_callers_reuse_subprocess_and_guard(self):
        for name in ('backup-test', 'inventory-test'):
            xml = '''<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:b="urn:biomerte:v1"><process id="caller" isExecutable="true" b:inputs='{"request_id":"str","workflow_instance_id":"str","proposal":"obj","question":"str","logical_operation":"str"}' b:outputs='{"outcome":"str"}'>
            <startEvent id="start"/><sequenceFlow id="f1" sourceRef="start" targetRef="permission"/>
            <callActivity id="permission" calledElement="biome-authorization" b:args='{"request_id":{"$":"input.request_id"},"workflow_instance_id":{"$":"input.workflow_instance_id"},"proposal":{"$":"input.proposal"},"question":{"$":"input.question"}}' b:save="permit"/>
            <sequenceFlow id="f2" sourceRef="permission" targetRef="choice"/><exclusiveGateway id="choice" default="no"/>
            <sequenceFlow id="yes" sourceRef="choice" targetRef="execute"><conditionExpression>{"op":"eq","args":[{"$":"permit.outcome"},"approved"]}</conditionExpression></sequenceFlow>
            <sequenceFlow id="no" sourceRef="choice" targetRef="done"/>
            <serviceTask id="execute" b:contract="core.echo:1" b:resource="builtin.echo" b:leaseScope="activity" b:args='{"$":"input.proposal.action.inputs"}' b:authorization='{"id":{"$":"permit.authorization_id"},"proposal":{"$":"input.proposal"},"operation":{"$":"input.logical_operation"}}'/>
            <sequenceFlow id="f3" sourceRef="execute" targetRef="done"/><endEvent id="done" b:result='{"outcome":{"$":"permit.outcome"}}'/></process></definitions>'''
            self.call('bpmn.import', dict(name=name, xml=xml))
        self.call('authorization.protect', dict(contract='core.echo:1', resource='builtin.echo'))
        runs = []
        for i, name in enumerate(('backup-test', 'inventory-test')):
            args = dict(request_id='caller-request-' + str(i), workflow_instance_id='independent-' + str(i),
                        proposal=proposal(str(i)), question='Allow test ' + str(i), logical_operation='caller-op-' + str(i))
            runs.append(self.call('process.start', dict(name=name, inputs=args))['id'])
        self.drain()
        rows = self.inbox()
        self.assertEqual(2, len(rows))
        for row in rows:
            self.respond(row, 'approve' if row['request']['id'].endswith('0') else 'reject')
        self.drain()
        results = [self.call('process.get', dict(run=r)) for r in runs]
        self.assertEqual(['completed', 'completed'], [r['state'] for r in results])
        self.assertEqual(['approved', 'rejected'], [r['result']['outcome'] for r in results])
        self.assertEqual('succeeded', self.call('cap.status', dict(operation='caller-op-0'))['state'])
        with self.assertRaises(Fault):
            self.call('cap.status', dict(operation='caller-op-1'))

    def test_text_conditions_and_questions_require_clarification(self):
        for i, (choice, text) in enumerate((('approve', 'Only after backing up'), ('', 'Which disk?'), ('approve', 'Yes, but exclude my photos'))):
            run = self.start(request='text-' + str(i), workflow='text-' + str(i))
            row = next(r for r in self.inbox() if r['request']['id'] == 'text-' + str(i))
            response = self.respond(row, choice, text)
            self.drain()
            result = self.call('process.get', dict(run=run))['result']
            self.assertEqual('clarification_required', result['outcome'])
            self.assertEqual('', result['authorization_id'])
            self.assertEqual(text, self.call('decision.get', dict(id=row['id']))['response']['text'])
            self.assertEqual(text, response['text'])

    def test_no_response_never_authorizes_and_defer_is_explicit(self):
        run = self.start()
        self.drain()
        self.assertEqual('running', self.call('process.get', dict(run=run))['state'])
        self.respond(self.inbox()[0], 'defer')
        self.drain()
        self.assertEqual('deferred', self.call('process.get', dict(run=run))['result']['outcome'])
        self.assertEqual([], self.app.stores['site'].execute(lambda tx: tx.rows('authorization_grants')))

    def test_old_proposal_and_altered_effects_rejected(self):
        grant, _ = self.grant()
        altered = proposal(); altered['expected_effects'].append('delete files')
        with self.assertRaises(Fault):
            self.call('authorization.dispatch', dict(id=grant, proposal=altered, operation='bad-effects'))
        self.start(proposal('new value', revision=2))
        with self.assertRaises(Fault):
            self.call('authorization.dispatch', dict(id=grant, proposal=proposal(), operation='obsolete'))
        self.assertFalse(self.call('authorization.get', dict(id=grant))['active'])

    def test_revocation_and_unapproved_direct_dispatch_are_blocked(self):
        grant, _ = self.grant()
        self.call('authorization.protect', dict(contract='core.echo:1', resource='builtin.echo'))
        with self.assertRaises(Fault):
            self.call('cap.submit', dict(contract='core.echo:1', resource='builtin.echo', inputs={'value': 'bypass'}, operation='bypass'))
        self.call('authorization.revoke', dict(id=grant, reason='User withdrew permission'))
        with self.assertRaises(Fault):
            self.call('authorization.dispatch', dict(id=grant, proposal=proposal(), operation='revoked'))

    def test_single_use_atomic_race(self):
        grant, _ = self.grant()
        barrier = threading.Barrier(2)
        results = []
        def worker(n):
            barrier.wait()
            try:
                r = self.call('authorization.dispatch', dict(id=grant, proposal=proposal(), operation='race-' + str(n)))
                results.append(r['state'])
            except Fault:
                results.append('blocked')
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(['succeeded', 'blocked'], results)

    def test_response_requires_real_phone_signature_and_exact_revision(self):
        self.start()
        row = self.inbox()[0]
        with self.assertRaises(Fault):
            self.call('authorization.resolve', dict(request_id='request-1', decision={'outcome': 'approve', 'authenticated': True, 'decision_id': row['id']}))
        stale = copy.deepcopy(row); stale['request']['proposal_revision'] += 1
        with self.assertRaises(Fault): self.respond(stale)
        e = dict(device=self.device, site=self.app.site, id=uid(), command='inbox', payload='e30=')
        e['signature'] = sign(self.phone_key, e)
        e['command'] = 'respond'
        with self.assertRaises(Fault): self.call('decision.mobile', dict(envelope=e))
        self.call('decision.device.revoke', dict(device=self.device, reason='Lost phone'))
        with self.assertRaises(Fault): self.respond(row)

    def test_expiry_is_explicit_and_standing_permission_is_enumerated(self):
        p = proposal(kind='standing')
        grant, _ = self.grant(p)
        g = self.call('authorization.get', dict(id=grant))
        self.assertNotIn('expires_at', g)
        for n in range(2):
            self.call('authorization.dispatch', dict(id=grant, proposal=p, operation='standing-' + str(n)))
        self.assertEqual(2, len(self.call('authorization.get', dict(id=grant))['usage']))
        p2 = proposal('new', revision=2); p2['expires_at'] = 1
        self.start(p2)
        row = self.inbox()[0]
        response = self.respond(row)
        resolved = self.call('authorization.resolve', dict(request_id='request-1', decision=response))
        with self.assertRaises(Fault):
            self.call('authorization.dispatch', dict(id=resolved['authorization_id'], proposal=p2, operation='expired'))

    def test_conditional_check_is_revalidated_at_dispatch(self):
        self.call('knowledge.put', dict(key='precondition', body={'backed_up': True}))
        expected = self.call('knowledge.get', dict(key='precondition'))
        p = proposal(); p['conditions'] = [dict(description='Verified backup', operation='knowledge.get', arguments={'key': 'precondition'}, expected=expected)]
        grant, _ = self.grant(p)
        self.call('knowledge.put', dict(key='precondition', body={'backed_up': False}))
        with self.assertRaises(Fault):
            self.call('authorization.dispatch', dict(id=grant, proposal=p, operation='condition-not-met'))
        self.assertEqual([], self.call('authorization.get', dict(id=grant))['usage'])

    def test_cancel_pending_question_withdraws_without_unknown_effect(self):
        run = self.start()
        row = self.inbox()[0]
        self.call('process.cancel', dict(run=run))
        self.assertEqual([], self.inbox())
        with self.assertRaises(Fault): self.respond(row)

    def test_revocation_between_reservation_and_worker_claim_stops_dispatch(self):
        self.call('cap.define', dict(name='protected.test', inputs={'value':'str'}, outputs={'value':'str'}))
        self.call('cap.offer', dict(provider='test-provider', contract='protected.test:1', resource='test.resource'))
        p = proposal(); p['action'] = dict(contract='protected.test:1', resource='test.resource', inputs={'value':'one'})
        grant, _ = self.grant(p)
        a = self.call('authorization.dispatch', dict(id=grant, proposal=p, operation='queued-protected'))
        self.call('cap.fenced', dict(provider='test-provider', lease=a['lease'], generation=a['generation']))
        self.call('authorization.revoke', dict(id=grant, reason='Stop before dispatch'))
        result = self.call('provider.claim', dict(provider='test-provider', operation=a['id'], generation=a['generation']))
        self.assertEqual('failed', result['state'])
        self.assertTrue(result['result']['authorization_blocked'])
        self.assertEqual('failed', self.call('authorization.get', dict(id=grant))['usage'][0]['state'])

    def test_uncertain_execution_retains_identity_and_cannot_dispatch_again(self):
        self.call('cap.define', dict(name='uncertain.test', inputs={'value':'str'}, outputs={'value':'str'}))
        self.call('cap.offer', dict(provider='test-provider', contract='uncertain.test:1', resource='test.resource'))
        p=proposal();p['action']=dict(contract='uncertain.test:1', resource='test.resource', inputs={'value':'one'})
        grant, _ = self.grant(p)
        a=self.call('authorization.dispatch', dict(id=grant, proposal=p, operation='uncertain-op'))
        self.call('cap.fenced', dict(provider='test-provider', lease=a['lease'], generation=a['generation']))
        self.call('provider.claim', dict(provider='test-provider', operation=a['id'], generation=a['generation']))
        self.call('provider.result', dict(provider='test-provider', operation=a['id'], generation=a['generation'], state='uncertain', result={'reason':'Connection lost after side effect'}))
        same=self.call('authorization.dispatch', dict(id=grant, proposal=p, operation='uncertain-op'))
        self.assertEqual('uncertain', same['state'])
        with self.assertRaises(Fault):self.call('authorization.dispatch', dict(id=grant, proposal=p, operation='second-uncertain-op'))
        self.call('provider.result', dict(provider='test-provider', operation=a['id'], generation=a['generation'], state='succeeded', result={'value':'one'}))
        self.assertEqual('succeeded', self.call('authorization.get', dict(id=grant))['usage'][0]['state'])

    def test_general_decision_is_not_an_authorization(self):
        request=dict(id='general-question',workflow_instance_id='any-workflow',proposal_revision=1,
            question='Which colour?',choices=[dict(id='green',label='Green',meaning='approve')])
        a=self.call('cap.submit',dict(contract='human.decision:1',resource='human.decisions',inputs={'request':request},operation='general-decision'))
        self.respond(self.inbox()[0],'green')
        self.assertEqual('approve',self.call('cap.status',dict(operation=a['id']))['result']['decision']['outcome'])
        self.assertEqual([],self.app.stores['site'].execute(lambda tx:tx.rows('authorization_grants')))

    def test_group_scope_limits_each_explicit_action_to_one_logical_use(self):
        p=proposal(kind='group')
        second=dict(contract='core.echo:1',resource='builtin.echo',inputs={'value':'second'})
        p['scope']['actions'].append(second)
        grant,_=self.grant(p)
        self.call('authorization.dispatch',dict(id=grant,proposal=p,operation='group-one'))
        p2=copy.deepcopy(p);p2['action']=second
        self.call('authorization.dispatch',dict(id=grant,proposal=p2,operation='group-two'))
        with self.assertRaises(Fault):self.call('authorization.dispatch',dict(id=grant,proposal=p2,operation='group-three'))
