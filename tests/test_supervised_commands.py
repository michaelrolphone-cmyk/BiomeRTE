import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from biomerte import command_runner as runner
from biomerte import supervised_commands as supervised
from biomerte.worker import Worker
from biomerte.store import digest, encode, Fault


class Remote:
    site, realm, node = 'site', 'site', 'provider'
    def __init__(self):
        self.results = {}
        self.lose_ack = False
        self.heartbeats = 0
    def call(self, name, args, rid=None):
        if name == 'provider.claim':
            return dict(state='claimed', output_schema={'ok': {'type':'bool','required':True}})
        if name == 'cap.command.state':
            return {'state': 'claimed', 'kill_requested': False}
        if name == 'provider.custody':
            self.heartbeats += 1
            return {}
        if name == 'cap.heartbeat':
            self.heartbeats += 1
            return {}
        if name == 'provider.result':
            result = dict(id=args['operation'], provider=self.node, generation=args['generation'],
                          state=args['state'], result=args['result'])
            self.results[args['operation']] = result
            if self.lose_ack:
                self.lose_ack = False
                raise IOError('lost acknowledgement after durable acceptance')
            return result
        if name == 'cap.status':
            return self.results.get(args['operation'], {})
        return {}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
    def wait(self, directory):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = runner.inspect(directory)
            if result['state'] not in ('starting', 'launching', 'running'):
                return result
            time.sleep(.03)
        self.fail('runner did not complete')
    def argv(self, delay=.1):
        marker = os.path.join(self.root, 'effects')
        script = ("import json,time; "
                  "open(%r,'a').write('once\\n'); time.sleep(%r); print(json.dumps({'ok':True}))") % (marker, delay)
        return [sys.executable, '-S', '-c', script]
    def test_duplicate_ensure_executes_once(self):
        argv, request = self.argv(), {'operation': 'one', 'inputs': {}}
        for _ in range(5):
            runner.ensure(self.root, 'one', 'sig', argv, request)
        directory = runner.location(self.root, 'one')
        self.assertEqual(self.wait(directory)['state'], 'finished')
        self.assertEqual(self.text(os.path.join(self.root, 'effects')), 'once\n')
        with self.assertRaises(ValueError):
            runner.ensure(self.root, 'one', 'different', argv, request)
    def test_runner_survives_requesting_process_death(self):
        argv = self.argv(.4)
        script = ("from biomerte.command_runner import ensure; import os; "
                  "ensure(%r,'survive','sig',%r,{'operation':'survive'}); os._exit(0)") % (self.root, argv)
        subprocess.check_call([sys.executable, '-S', '-c', script])
        directory = runner.location(self.root, 'survive')
        self.assertEqual(self.wait(directory)['returncode'], 0)
        self.assertEqual(self.text(os.path.join(self.root, 'effects')), 'once\n')
    def test_dead_custodian_never_replays_launch(self):
        directory = runner.location(self.root, 'dead')
        os.makedirs(directory)
        runner.save(os.path.join(directory, 'state.json'), {'state': 'running', 'adapter_pid': 123})
        status = runner.ensure(self.root, 'dead', 'sig', self.argv(), {'operation': 'dead'})
        self.assertEqual(status['state'], 'interrupted')
        self.assertFalse(status['replay_allowed'])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'effects')))
    def text(self, path):
        with open(path) as source:
            return source.read()
    def test_worker_process_death_reattaches_without_replay(self):
        script = ("import sys; from tests.test_supervised_commands import RunnerTests,Remote; "
                  "t=RunnerTests(); t.root=%r; "
                  "w,o=t.worker(Remote()); "
                  "o['argv']=t.argv(1.2); w.offers=[o]; w.perform(t.action())") % self.root
        process = subprocess.Popen([sys.executable, '-S', '-c', script])
        deadline = time.monotonic() + 10
        while not os.path.exists(os.path.join(self.root, 'effects')):
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail('worker failed before adapter launch')
            time.sleep(.02)
        process.kill()
        process.wait()
        remote = Remote()
        worker, _ = self.worker(remote)
        try:
            worker.perform(supervised.pending(worker)[0])
            self.assertEqual(self.text(os.path.join(self.root, 'effects')), 'once\n')
            self.assertEqual(supervised.pending(worker), [])
        finally:
            worker.db.close()
    def test_admission_waits_for_pending_custody(self):
        remote = Remote()
        worker, offer = self.worker(remote)
        action = self.action()
        signature = digest([action[k] for k in ('contract','inputs','resource','generation','lease')])
        worker.db.execute('INSERT INTO supervised_pending VALUES(?,?,?,?,?)',
                          ('command',signature,encode(action),encode(offer),encode({})))
        remote.call = lambda name,args=None,rid=None: {'fences':[dict(id='new',guard='slot',resource='terminal')],
                                                      'actions':[]} if name=='provider.poll' else {}
        class Pool:
            failures, running = {}, {}
            def dispatch(self, actions, fences=()):
                self.actions, self.fences = actions, fences
                return 0
        pool = Pool()
        try:
            worker.once(pool)
            self.assertEqual(pool.fences, [])
            self.assertEqual(pool.actions[0]['id'], 'command')
        finally:
            worker.db.close()
    def test_launch_failure_is_known_not_executed(self):
        runner.ensure(self.root,'missing','sig',['/no/such/executable'],{})
        status = self.wait(runner.location(self.root,'missing'))
        self.assertEqual(status['state'], 'not_started')
    def test_adapter_failure_retains_evidence(self):
        runner.ensure(self.root, 'bad', 'sig', [sys.executable, '-S', '-c',
                      "import sys; sys.stderr.write('failure'); sys.exit(7)"], {})
        directory = runner.location(self.root, 'bad')
        self.assertEqual(self.wait(directory)['returncode'], 7)
        self.assertEqual(self.text(os.path.join(directory, 'stderr')), 'failure')
    def worker(self, remote):
        offer = dict(contract='host.terminal.run:1', resource='terminal', argv=self.argv())
        worker = Worker(os.path.join(self.root, 'worker.sqlite'), remote, [offer], None)
        worker.db.execute('INSERT OR REPLACE INTO fences VALUES(?,?,?)', ('slot', 1, 'lease'))
        return worker, offer
    def action(self):
        return dict(id='command', provider='provider', contract='host.terminal.run:1', inputs={},
                    resource='terminal', guard='slot', generation=1, lease='lease')
    def test_lost_ack_restart_redelivers_without_execution(self):
        remote = Remote()
        remote.lose_ack = True
        worker, offer = self.worker(remote)
        action = self.action()
        with self.assertRaises(IOError):
            worker.perform(action)
        self.assertEqual(len(supervised.pending(worker)), 1)
        worker.db.close()
        worker, _ = self.worker(remote)
        try:
            # Simulate an empty controller inbox: the local pending index owns recovery.
            worker.perform(supervised.pending(worker)[0])
            self.assertEqual(supervised.pending(worker), [])
            self.assertEqual(self.text(os.path.join(self.root, 'effects')), 'once\n')
            self.assertEqual(remote.results['command']['state'], 'succeeded')
            directory = runner.location(os.path.join(self.root, 'command-runners'), 'command')
            self.assertFalse(os.path.exists(os.path.join(directory, 'stdout')))
            self.assertTrue(os.path.exists(os.path.join(directory, 'acknowledged.json')))
        finally:
            worker.db.close()
    def test_restart_attaches_running_adapter(self):
        remote = Remote()
        worker, offer = self.worker(remote)
        action = self.action()
        signature = digest([action[k] for k in ('contract','inputs','resource','generation','lease')])
        worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                          ('command', signature, 'uncertain', '{}', 'slot'))
        worker.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', ('command', signature))
        worker.db.execute('INSERT INTO supervised_pending VALUES(?,?,?,?,?)',
                          ('command', signature, encode(action), encode(offer), encode({'ok':{'type':'bool','required':True}})))
        root = os.path.join(self.root, 'command-runners')
        directory = runner.location(root, 'command')
        request = dict(operation='command', inputs={}, progress_path=os.path.join(directory,'progress.json'))
        runner.ensure(root,'command',signature,offer['argv'],request)
        worker.db.close()
        worker, _ = self.worker(remote)
        try:
            worker.perform(action)
            self.assertEqual(self.text(os.path.join(self.root, 'effects')), 'once\n')
            self.assertGreater(remote.heartbeats, 0)
        finally:
            worker.db.close()
    def test_acknowledged_runner_receipts_expire_with_action(self):
        remote = Remote()
        worker, offer = self.worker(remote)
        try:
            worker.perform(self.action())
            directory = runner.location(os.path.join(self.root, 'command-runners'), 'command')
            self.assertTrue(os.path.exists(os.path.join(directory,'acknowledged.json')))
            worker.db.execute("DELETE FROM actions WHERE id='command'")
            supervised.cleanup_expired(worker)
            self.assertFalse(os.path.exists(directory))
            self.assertEqual(list(worker.db.execute('SELECT id FROM supervised_delivery')), [])
        finally:
            worker.db.close()
    def test_ack_cleanup_retry_does_not_republish_empty_result(self):
        remote = Remote()
        worker, offer = self.worker(remote)
        original = worker.cleanup_results
        with mock.patch.object(worker, 'cleanup_results', side_effect=IOError('cleanup interrupted')):
            with self.assertRaises(IOError):
                worker.perform(self.action())
        try:
            worker.perform(self.action())
            self.assertEqual(remote.results['command']['result'], {'ok': True})
            self.assertEqual(supervised.pending(worker), [])
        finally:
            worker.db.close()


class LateCompletionTests(unittest.TestCase):
    def test_late_result_does_not_revive_lease_or_emit_success(self):
        from biomerte.app import initialize, Application
        from biomerte.store import uid
        with tempfile.TemporaryDirectory() as home:
            initialize(home)
            app = Application(home)
            def call(name, arguments=None):
                return app.call(name, arguments or {}, uid(), 'site')
            provider = app.config['node']
            call('cap.define', dict(name='host.terminal.run', inputs={'argv':'list'}, outputs={'ok':'bool'}))
            call('cap.offer', dict(provider=provider, contract='host.terminal.run:1', resource='terminal'))
            lease = call('cap.acquire', dict(task='old', contract='host.terminal.run:1', resource='terminal'))
            call('cap.fenced', dict(provider=provider, lease=lease['id'], generation=lease['generation']))
            call('cap.invoke', dict(lease=lease['id'], operation='late', inputs={'argv':[]}))
            # Cancel before claim to force abandonment without depending on a clock.
            call('cap.kill', dict(operation='late'))
            current = call('cap.acquire', dict(task='new', contract='host.terminal.run:1', resource='terminal'))
            call('cap.fenced', dict(provider=provider, lease=current['id'], generation=current['generation']))
            args = dict(provider=provider, operation='late', generation=lease['generation'],
                        state='succeeded', result={'ok':True})
            outcome = call('provider.result', args)
            self.assertEqual(outcome['state'], 'abandoned')
            self.assertEqual(outcome['execution_state'], 'succeeded')
            self.assertEqual(call('provider.result', args), outcome)
            self.assertEqual(call('cap.lease', {'lease':current['id']})['state'], 'bound')
            with self.assertRaises(Fault):
                call('provider.result', dict(args, result={'ok':False}))
            app.close() if hasattr(app, 'close') else None


if __name__ == '__main__':
    unittest.main()
