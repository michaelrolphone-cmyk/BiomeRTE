import os
import tempfile
import unittest

from biomerte.app import Application, initialize
from biomerte.store import Fault, uid
from biomerte.xmlprofile import compile_xml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = 'capability-followup'


class CapabilityFollowup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        initialize(self.home)
        self.app = Application(self.home)
        with open(os.path.join(ROOT, 'examples', NAME + '.bpmn')) as stream:
            self.xml = stream.read()
        self.call('bpmn.import', {'name': NAME, 'xml': self.xml})
        self.define('qa.command', {'argv':'list'}, {'exit_code':'int','text':'str'}, 'host', 'pi')
        self.define('agent.future.delegate', {'request':'str','context':'obj'}, {'result':'obj'}, 'reader', 'reader-7')

    def call(self, name, args, rid=None):
        return self.app.call(name, args, rid or uid())

    def define(self, name, inputs, outputs, provider, resource):
        c = self.call('cap.define', dict(name=name, inputs=inputs, outputs=outputs))
        return self.call('cap.offer', dict(contract=c['id'], resource=resource, provider=provider))

    def settle(self):
        for _ in range(60):
            if not self.app.tick('site'):
                return
        self.fail('Did not reach a durable wait')

    def launch(self, **extra):
        args = dict(contract='qa.command:1', arguments={'argv':['command']}, resource='pi',
                    caller='agent.future.delegate:1', caller_resource='reader-7',
                    context={'thread':'original-thread'}, success_prompt='continue success', failure_prompt='continue failure')
        args.update(extra)
        run = self.call('cap.run_async', args, 'same-launch')
        self.assertEqual(run, self.call('cap.run_async', args, 'same-launch'))
        self.settle()
        return run['run_id']

    def poll(self, provider):
        return self.call('provider.poll', {'provider':provider})

    def claim(self, provider):
        for f in self.poll(provider)['fences']:
            self.call('cap.fenced', dict(provider=provider, lease=f['id'], generation=f['generation']))
        action, = self.poll(provider)['actions']
        self.call('provider.claim', dict(provider=provider, operation=action['id'], generation=action['generation']))
        return action

    def finish(self, a, state, result):
        self.call('provider.result', dict(provider=a['provider'], operation=a['id'], generation=a['generation'],
                                          state=state, result=result))
        self.settle()

    def test_success_restart_queue_and_correlated_poll_receiver(self):
        held = self.call('cap.acquire', dict(contract='qa.command:1', resource='pi', task='other'))
        run = self.launch()
        self.assertFalse(self.poll('host')['actions'])
        self.call('cap.release', dict(lease=held['id']))
        self.settle()
        job = self.claim('host')
        self.app = Application(self.home)
        self.settle()
        self.assertEqual([a['id'] for a in self.poll('host')['actions']], [job['id']])
        self.assertFalse(self.poll('reader')['actions'])
        self.finish(job, 'succeeded', {'exit_code':0,'text':'stdout'})
        self.assertEqual(self.call('cap.lease', {'lease':job['lease']})['state'], 'released')
        cb = self.claim('reader')
        ctx = cb['inputs']['context']
        self.assertEqual(cb['inputs']['request'], 'continue success')
        self.assertEqual(ctx['caller_context'], {'thread':'original-thread'})
        self.assertEqual(ctx['outcome']['run_id'], run)
        self.assertEqual(ctx['outcome']['result']['text'], 'stdout')
        self.assertEqual(ctx['outcome']['operation_id'], job['id'])
        self.finish(cb, 'succeeded', {'result':{'ack':True}})
        self.assertEqual(self.call('process.get', {'run':run})['state'], 'completed')
        self.assertEqual(self.call('cap.lease', {'lease':cb['lease']})['state'], 'released')

    def test_nonzero_and_large_output_reference(self):
        self.launch()
        job = self.claim('host')
        out = dict(exit_code=17, text='X'*40000)
        self.finish(job, 'succeeded', out)
        cb = self.claim('reader')['inputs']
        o = cb['context']['outcome']
        self.assertEqual(cb['request'],'continue failure')
        self.assertEqual(o['provider_state'],'succeeded')
        self.assertFalse(o['ok'])
        self.assertFalse(o['result_inline'])
        self.assertTrue(o['result_preview'])
        self.assertEqual(self.call(o['result_ref']['operation'],o['result_ref']['arguments'])['result'], out)

    def test_uncertain_waits_for_reconciliation_and_provider_failure_is_preserved(self):
        self.launch()
        job = self.claim('host')
        self.finish(job, 'uncertain', {'reason':'response lost'})
        self.assertFalse(self.poll('reader')['actions'])
        self.finish(job, 'failed', {'reason':'confirmed failure','stderr':'detail'})
        cb = self.claim('reader')['inputs']
        self.assertEqual(cb['request'], 'continue failure')
        self.assertEqual(cb['context']['outcome']['result']['stderr'], 'detail')

    def test_invalid_arguments_route_to_failure_without_a_worker_call(self):
        self.launch(arguments={'wrong':'field'})
        self.assertFalse(self.poll('host')['actions'])
        cb = self.claim('reader')['inputs']
        self.assertEqual(cb['request'], 'continue failure')
        self.assertEqual(cb['context']['outcome']['state'], 'dispatch_failed')
        self.assertIn('wrong', cb['context']['outcome']['error'])

    def test_custom_predicate_and_push_agent_routine(self):
        self.call('routine.publish', dict(name='qa.push',inputs={'request':'str','context':'obj'},
            outputs={'result':'obj'},steps=[{'type':'delegate','to':'chatgpt','request':{'$':'input.request'},
            'context':{'$':'input.context'},'outputs':{'result':'obj'},'save':'reply'}],result={'$':'reply'}))
        self.call('routine.expose',dict(name='qa.push',capability='agent.qa.push',version=1))
        run = self.launch(contract='core.echo:1', resource='', arguments={'value':{'verified':False}},
                          success_equals={'value.verified':True}, caller='agent.qa.push:1',caller_resource='')
        event, = self.call('delegation.feed', {})['events']
        self.assertEqual(event['target'],'chatgpt')
        d = self.call('delegation.get', {'id':event['data']['delegation_id']})
        self.assertEqual(d['question'],'continue failure')
        self.assertEqual(d['context']['outcome']['run_id'],run)
        self.assertEqual(d['context']['caller_context']['thread'],'original-thread')

    def test_callback_failure_never_repeats_work(self):
        run = self.launch(contract='core.echo:1', resource='', arguments={'value':'done'})
        callback = self.claim('reader')
        self.finish(callback, 'failed', {'error':'receiver declined'})
        self.assertEqual(self.call('process.get', {'run':run})['state'], 'failed')
        self.assertEqual(self.call('cap.lease', {'lease':callback['lease']})['state'], 'released')
        self.app = Application(self.home)
        self.settle()
        self.assertEqual(self.poll('reader')['actions'], [])

    def test_optional_addresses_and_explicit_success_condition(self):
        run = self.call('cap.run_async', dict(contract='core.echo:1', arguments={'value':'x'},
            caller='agent.future.delegate:1', success_prompt='yes', failure_prompt='no',
            success_equals={'value':'x'}), 'launch-defaults')
        self.settle()
        cb = self.claim('reader')['inputs']
        self.assertEqual(cb['request'], 'yes')
        self.assertEqual(cb['context']['caller_context'], {})
        self.assertEqual(cb['context']['outcome']['run_id'], run['run_id'])

    def test_invalid_callback_does_not_start_work(self):
        with self.assertRaises(Fault):
            self.launch(caller='core.echo:1')
        self.assertFalse(self.poll('host')['actions'])
        with self.assertRaises(Fault):
            compile_xml(self.xml.replace('b:contractRef="input.contract"',
                                          'b:contractRef="input.contract" b:operation="bus.send"'))
