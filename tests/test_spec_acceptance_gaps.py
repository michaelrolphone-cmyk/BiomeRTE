"""Acceptance regressions for lifetime, ownership and operational retention."""
import tempfile
import unittest
from unittest.mock import patch

from biomerte.app import Application, initialize
from biomerte.api import Context
from biomerte.store import Fault, uid
from biomerte import providers, lifecycle, retention


class SpecAcceptanceGaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.now = 10000.0
        clock = patch('time.time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.call('cap.define', name='sample', inputs={}, outputs={})

    def call(self, op, **args):
        return self.app.call(op, args, uid())

    def offer(self, cls='command', **cfg):
        return self.call('cap.offer', provider='test', contract='sample:1', resource='slot',
                         configuration=dict(cfg, lease_class=cls))

    def acquire(self, task='one'):
        return self.call('cap.acquire', task=task, contract='sample:1', resource='slot')

    def claimed(self, cls='command', release=None, **cfg):
        self.offer(cls, **cfg)
        lease = self.acquire()
        self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        args = dict(lease=lease['id'], inputs={}, operation='one')
        if release is not None:
            args['release_after_result'] = release
        self.call('cap.invoke', **args)
        action = self.call('provider.claim', provider='test', operation='one', generation=lease['generation'])
        return lease, action

    def test_persistent_cannot_be_changed_to_a_watched_command(self):
        lease, action = self.claimed('persistent')
        self.offer('command', watch_ttl_seconds=1)
        self.app = Application(self.tmp.name)
        self.now += 10000
        self.app.tick('site')
        self.assertEqual('claimed', self.call('cap.status', operation='one')['state'])
        self.call('provider.result', provider='test', operation='one', generation=lease['generation'],
                  state='succeeded', result={})
        self.app.tick('site')
        self.assertEqual('bound', self.call('cap.lease', lease=lease['id'])['state'])
        self.assertEqual('persistent', action['lease_policy']['lease_class'])

    def test_command_watch_cannot_be_removed_or_extended_by_offer_edit(self):
        self.claimed('command', watch_ttl_seconds=10)
        self.offer('persistent', watch_ttl_seconds=900)
        self.now += 11
        self.app.tick('site')
        self.assertEqual('abandoned', self.call('cap.status', operation='one')['state'])

    def test_long_command_retains_original_heartbeat_intervals(self):
        lease, _ = self.claimed('long_command', heartbeat_interval_seconds=10, missed_heartbeats_limit=3)
        self.offer('command', watch_ttl_seconds=1)
        self.now += 20
        self.app.tick('site')
        self.assertEqual('claimed', self.call('cap.status', operation='one')['state'])
        self.call('cap.heartbeat', provider='test', operation='one', generation=lease['generation'])
        self.now += 31
        self.app.tick('site')
        self.assertEqual('abandoned', self.call('cap.status', operation='one')['state'])

    def test_fence_ack_deadline_is_pinned(self):
        self.offer('persistent', fence_ttl_seconds=5)
        lease = self.acquire()
        self.offer('persistent', fence_ttl_seconds=900)
        self.now += 6
        self.app.tick('site')
        self.assertEqual('released', self.call('cap.lease', lease=lease['id'])['state'])

    def test_explicit_false_release_override_survives_recovery_tick(self):
        lease, _ = self.claimed(release=False)
        self.call('provider.result', provider='test', operation='one', generation=lease['generation'],
                  state='succeeded', result={})
        self.app.tick('site')
        self.assertEqual('bound', self.call('cap.lease', lease=lease['id'])['state'])

    def test_paired_server_can_release_only_its_own_lease(self):
        self.offer('persistent')
        a = Context(self.app, actor='server-a', permissions={'cap.acquire', 'cap.release'})
        b = Context(self.app, actor='server-b', permissions={'cap.acquire', 'cap.release'})
        store = self.app.stores['site']
        lease = store.execute(lambda tx: providers.acquire(tx, a, 'shared-task', 'sample:1', 'slot'))
        self.assertEqual('server-a', lease['owner'])
        with self.assertRaises(Fault) as error:
            store.execute(lambda tx: providers.release_lease(tx, b, lease['id']))
        self.assertEqual(403, error.exception.status)
        with self.assertRaises(Fault):
            store.execute(lambda tx: providers.acquire(tx, b, 'shared-task', 'sample:1', 'slot'))
        released = store.execute(lambda tx: providers.release_lease(tx, a, lease['id']))
        self.assertEqual('released', released['state'])

    def test_promoted_notifications_expire_without_creating_user_knowledge(self):
        self.offer('persistent')
        first = self.acquire()
        second = self.acquire('second')
        self.assertEqual('queued', second['state'])
        with self.app.stores['site'].connect() as db:
            before = db.execute("SELECT COUNT(*) FROM records WHERE kind='documents'").fetchone()[0]
        self.call('cap.release', lease=first['id'])
        second = self.call('cap.lease', lease=second['id'])
        self.call('cap.fenced', provider='test', lease=second['id'], generation=second['generation'])
        messages = self.call('bus.inbox', address='second')
        self.assertEqual(1, len(messages))
        self.assertGreater(messages[0]['expires_at'], self.now)
        with self.app.stores['site'].connect() as db:
            after = db.execute("SELECT COUNT(*) FROM records WHERE kind='documents'").fetchone()[0]
        self.assertEqual(before, after)
        self.now += 901
        self.app.tick('site')
        self.assertEqual([], self.call('bus.inbox', address='second'))

    def test_operational_churn_expires_targets_and_exact_retention_markers(self):
        store = self.app.stores['site']
        def seed(tx):
            with retention.tracking(tx, self.now):
                for i in range(256):
                    key = 'churn-%04d' % i
                    tx.put('actions', key, dict(id=key, lease='old', generation=1,
                        state='succeeded', destination='', result={}, expires_at=self.now + 900))
        store.execute(seed)
        with store.connect() as db:
            self.assertEqual(256, db.execute("SELECT COUNT(*) FROM records WHERE kind='_retention' AND id LIKE 'actions:churn-%'").fetchone()[0])
        self.now += 901
        for _ in range(5):
            store.execute(lambda tx: lifecycle.sweep(tx, Context(self.app), self.now))
        with store.connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM records WHERE kind='actions' AND id LIKE 'churn-%'").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM records WHERE kind='_retention' AND id LIKE 'actions:churn-%'").fetchone()[0])

    def test_known_launch_failure_can_fence_after_lost_result_and_abandon(self):
        import os
        from biomerte.worker import Worker
        lease, action = self.claimed()
        app = self.app
        class Remote:
            node, site, realm = 'test', 'site', 'site'
            def call(inner, name, args, rid=None):
                if name == 'provider.result':
                    raise ConnectionError('simulated missing delivery')
                return app.call(name, args, rid or uid())
        worker = Worker(os.path.join(self.tmp.name, 'worker.sqlite'), Remote(),
                        [{'contract': 'sample:1', 'resource': 'slot', 'argv': ['/absent/adapter']}])
        self.addCleanup(worker.db.close)
        with self.assertRaises(ConnectionError):
            worker.perform(action)
        self.assertEqual('failed', worker.db.execute('SELECT state FROM actions WHERE id=?', ('one',)).fetchone()[0])
        waiting = self.acquire('replacement')
        self.now += 121
        self.app.tick('site')
        self.assertEqual('abandoned', self.call('cap.status', operation='one')['state'])
        replacement = self.call('cap.lease', lease=waiting['id'])
        worker.fence(replacement)
        self.assertEqual('bound', self.call('cap.lease', lease=waiting['id'])['state'])

    def test_output_stall_is_reported_without_revoking_live_custody(self):
        from biomerte.supervised_commands import OutputLiveness
        status = OutputLiveness(windows=3)
        self.assertFalse(status.observe({})[1]['stalled_output'])
        for _ in range(3):
            changed, checkpoint = status.observe({})
        self.assertTrue(changed)
        self.assertTrue(checkpoint['stalled_output'])
        self.assertFalse(status.observe({})[0])
        changed, checkpoint = status.observe({'signature': 'fresh-output', 'output': {'stdout': 'advanced'}})
        self.assertTrue(changed)
        self.assertFalse(checkpoint['stalled_output'])
        self.assertEqual('advanced', checkpoint['output']['stdout'])

    def test_worker_uses_authoritative_lease_policy_after_offer_and_adapter_change(self):
        import os
        import sys
        from biomerte.worker import Worker
        from biomerte import command_watch
        lease, action = self.claimed('long_command', heartbeat_interval_seconds=7)
        self.offer('command')
        app = self.app
        class Remote:
            node, site, realm = 'test', 'site', 'site'
            def call(inner, name, args, rid=None):
                return app.call(name, args, rid or uid())
        worker = Worker(os.path.join(self.tmp.name, 'worker.sqlite'), Remote(),
            [{'contract': 'sample:1', 'resource': 'slot', 'configuration': {'lease_class': 'command'},
              'argv': [sys.executable, '-c', 'import json,sys;json.load(sys.stdin);print("{}")']}])
        self.addCleanup(worker.db.close)
        original = command_watch.communicate
        policies = []
        def observed(w, proc, request, a, adapter):
            policies.append(adapter['configuration'])
            return original(w, proc, request, a, adapter)
        with patch.object(command_watch, 'communicate', side_effect=observed):
            worker.perform(action)
        self.assertEqual('long_command', policies[0]['lease_class'])
        self.assertEqual(7, policies[0]['heartbeat_interval_seconds'])
        self.assertEqual('succeeded', self.call('cap.status', operation='one')['state'])

    def test_abandoned_supervised_command_quiesces_before_replacement_fence(self):
        import os
        import sys
        import time
        import threading
        from pathlib import Path
        from biomerte.worker import Worker
        from biomerte import command_runner
        root = Path(self.tmp.name)
        started = root / 'adapter-started'
        adapter = root / 'adapter.py'
        adapter.write_text('import json,sys,time,pathlib\nr=json.load(sys.stdin)\n'
            + 'pathlib.Path(' + repr(str(started)) + ').write_text("once")\n'
            + 'stop=pathlib.Path(r["progress_path"]+".stop")\n'
            + 'deadline=time.monotonic()+8\nwhile not stop.exists() and time.monotonic()<deadline:time.sleep(.02)\n'
            + 'print(json.dumps({"ok":True}))\n')
        self.call('cap.define', name='host.terminal.run', inputs={'argv':'list'}, outputs={'ok':'bool'})
        self.call('cap.offer', provider='test', contract='host.terminal.run:1', resource='slot',
                  configuration={'lease_class':'command','watch_ttl_seconds':1,'heartbeat_interval_seconds':.1})
        lease = self.call('cap.acquire', task='custody', contract='host.terminal.run:1', resource='slot')
        self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        action = self.call('cap.invoke', lease=lease['id'], inputs={'argv':[]}, operation='custody')
        app = self.app
        no_heartbeat = threading.Event()
        class Remote:
            node, site, realm = 'test', 'site', 'site'
            def call(inner, name, args, rid=None):
                if name == 'cap.heartbeat' and no_heartbeat.is_set():
                    raise ConnectionError('lost custody heartbeat')
                return app.call(name, args, rid or uid())
        worker = Worker(str(root/'worker.sqlite'), Remote(), [{'contract':'host.terminal.run:1',
            'resource':'slot','argv':[sys.executable,str(adapter)]}])
        errors = []
        def execute():
            local_worker = Worker(worker.path, Remote(), worker.offers)
            try:local_worker.perform(action)
            except BaseException as error:errors.append(error)
            finally:local_worker.db.close()
        thread = threading.Thread(target=execute)
        thread.start()
        try:
            deadline = time.monotonic()+5
            while not started.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(started.exists(), repr(errors))
            waiting = self.call('cap.acquire', task='replacement', contract='host.terminal.run:1', resource='slot')
            no_heartbeat.set()
            time.sleep(.1)
            self.now += 2
            self.app.tick('site')
            self.assertEqual('abandoned', self.call('cap.status', operation='custody')['state'])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            result = self.call('cap.status', operation='custody')
            self.assertEqual('abandoned', result['state'])
            self.assertEqual('succeeded', result['execution_state'])
            self.assertEqual([], worker.db.execute('SELECT id FROM supervised_pending').fetchall())
            replacement = self.call('cap.lease', lease=waiting['id'])
            worker.fence(replacement)
            self.assertEqual('bound', self.call('cap.lease', lease=waiting['id'])['state'])
        finally:
            directory = command_runner.location(str(root/'command-runners'), action['id'])
            if os.path.isdir(directory):
                command_runner.save(os.path.join(directory,'progress.json.stop'), {'stop':True})
            thread.join(8)
            worker.db.close()

    def test_explicit_stop_terminates_uncooperative_long_command_group(self):
        import os
        import sys
        from biomerte.worker import Worker
        lease, action = self.claimed('long_command', heartbeat_interval_seconds=.1)
        app = self.app
        class Remote:
            node, site, realm = 'test', 'site', 'site'
            def call(inner, name, args, rid=None):
                if name == 'cap.command.state':
                    return {'state':'claimed','kill_requested':True}
                return app.call(name, args, rid or uid())
        worker = Worker(os.path.join(self.tmp.name, 'worker.sqlite'), Remote(),
            [{'contract':'sample:1','resource':'slot','argv':[sys.executable,'-c',
              'import sys,json,time;json.load(sys.stdin);time.sleep(30);print("{}")']}])
        self.addCleanup(worker.db.close)
        worker.perform(action)
        result = self.call('cap.status', operation='one')
        self.assertEqual('failed', result['state'])
        self.assertEqual('stop_requested', result['result']['evidence']['reason'])
        self.assertTrue(result['result']['evidence']['quiescent'])
