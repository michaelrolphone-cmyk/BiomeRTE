"""Regression coverage for independent Linux maintenance (Python 3.4+)."""
from __future__ import print_function

import hashlib
import json
import os
import queue
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.error import HTTPError, URLError

import linux_maintenance_agent as agent
import install_linux_maintenance as installer


JOB = {'id': 'one', 'argv': ['/bin/echo', 'hello'], 'cwd': None}
RESULT = {'state': 'succeeded', 'exit_code': 0, 'stdout': 'café 😀\n', 'stderr': ''}


class DurableExecutorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.events, self.started = queue.Queue(), []
        self.executor = agent.Executor(self.folder.name, self.events, self.started.append)
        self.addCleanup(self.executor.db.close)

    def test_commit_precedes_process_start_and_duplicate_never_reexecutes(self):
        self.assertEqual(self.executor.db.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertTrue(self.executor.admit(JOB))
        self.assertEqual(self.executor.db.execute(
            'SELECT state FROM commands WHERE id=?', ('one',)).fetchone(), ('running',))
        self.assertEqual(self.started, [JOB])
        self.assertFalse(self.executor.admit(JOB))
        self.assertEqual(self.started, [JOB])

    def test_commit_failure_cannot_start_a_process(self):
        connection = self.executor.db
        class FailingCommit(object):
            def __getattr__(self, name):
                return getattr(connection, name)
            def commit(self):
                raise sqlite3.OperationalError('simulated storage failure')
        self.executor.db = FailingCommit()
        with self.assertRaises(sqlite3.OperationalError):
            self.executor.admit(JOB)
        self.assertEqual(self.started, [])
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)

    def test_restart_marks_admitted_work_uncertain_and_never_replays(self):
        self.executor.admit(JOB)
        events, restarted = queue.Queue(), []
        other = agent.Executor(self.folder.name, events, restarted.append)
        self.addCleanup(other.db.close)
        receipt = events.get_nowait()
        self.assertEqual(receipt['id'], 'one')
        self.assertEqual(receipt['result']['state'], 'uncertain')
        self.assertFalse(other.admit(JOB))
        self.assertEqual(restarted, [])

    def test_changed_command_cannot_reuse_an_existing_identity(self):
        self.executor.admit(JOB)
        with self.assertRaises(ValueError):
            self.executor.admit(dict(JOB, argv=['/bin/echo', 'different']))
        self.assertEqual(self.started, [JOB])

    def test_actual_output_is_durable_until_matching_gateway_ack(self):
        self.executor.admit(JOB)
        self.executor.execute(JOB)
        result = self.events.get_nowait()['result']
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['stdout'], 'hello\n')
        self.assertEqual(result['stderr'], '')
        with self.assertRaises(ValueError):
            self.executor.acknowledge([('one', 'wrong')])
        self.assertEqual(self.executor.db.execute(
            'SELECT state FROM commands WHERE id=?', ('one',)).fetchone()[0], 'succeeded')
        self.executor.acknowledge([('one', agent.digest(result))])
        self.assertEqual(self.executor.db.execute(
            'SELECT state FROM commands WHERE id=?', ('one',)).fetchone()[0], 'reported')
        self.assertFalse(self.executor.admit(JOB))

    def test_output_failure_after_spawn_is_uncertain_not_unexecuted_failure(self):
        self.executor.admit(JOB)
        with mock.patch.object(agent, 'read_text', side_effect=IOError('storage unavailable')):
            self.executor.execute(JOB)
        result = self.events.get_nowait()['result']
        self.assertEqual(result['state'], 'uncertain')
        self.assertIn('pid', result)


class FakeTransport(object):
    host = 'pi-maintenance'
    def __init__(self):
        self.calls = []
        self.jobs = [JOB]
    def exchange(self, body):
        self.calls.append(body)
        if body['operation'] == 'heartbeat':
            return {'accepted': True, 'host': self.host}
        return {'jobs': self.jobs}
    def complete(self, identifier, result):
        self.calls.append({'operation': 'complete', 'id': identifier, 'result': result})
        return agent.digest(result)


class IndependentTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport, self.incoming, self.outgoing = FakeTransport(), queue.Queue(), queue.Queue()
        self.agent = agent.Agent(self.transport, self.incoming, self.outgoing)

    def snapshot(self):
        return {'version': 1, 'agent': agent.AGENT, 'boot_id': 'test-boot'}

    def test_heartbeat_does_not_wait_for_ledger_initialization(self):
        self.agent.step(self.snapshot())
        self.assertEqual([x['operation'] for x in self.transport.calls], ['heartbeat'])
        self.assertEqual(self.transport.calls[0]['diagnostics']['executor'],
                         'waiting_for_durable_storage')
        self.assertTrue(self.incoming.empty())

    def test_blocked_durable_admission_leaves_diagnostics_available_without_more_polling(self):
        self.outgoing.put({'kind': 'ready'})
        self.agent.step(self.snapshot())
        self.assertEqual(self.incoming.get_nowait(), {'kind': 'jobs', 'jobs': [JOB]})
        self.transport.calls = []
        # No ready reply: the executor may be in uninterruptible storage I/O.
        for unused in range(3):
            self.agent.step(self.snapshot())
        self.assertEqual([x['operation'] for x in self.transport.calls],
                         ['heartbeat', 'heartbeat', 'heartbeat'])
        self.assertTrue(self.incoming.empty())

    def test_failed_executor_cannot_be_reenabled_by_an_earlier_ready_message(self):
        self.outgoing.put({'kind': 'error', 'error': 'OperationalError'})
        self.outgoing.put({'kind': 'ready'})
        self.agent.step(self.snapshot())
        self.assertFalse(self.agent.ready)
        self.assertEqual([x['operation'] for x in self.transport.calls], ['heartbeat'])
        self.assertEqual(self.transport.calls[0]['diagnostics']['executor'], 'unavailable')

    def test_completed_result_does_not_disappear_on_lost_gateway_ack(self):
        self.outgoing.put({'kind': 'result', 'id': 'one', 'result': RESULT})
        with mock.patch.object(self.transport, 'complete', side_effect=IOError('lost ACK')):
            with self.assertRaises(IOError):
                self.agent.step(self.snapshot())
        self.assertEqual(self.agent.outbox, {'one': RESULT})
        self.agent.step(self.snapshot())
        self.assertEqual(self.agent.outbox, {})
        self.assertEqual(self.agent.acks, [('one', agent.digest(RESULT))])
        # Persistent state is not marked reported until the executor can commit.
        self.assertTrue(self.incoming.empty())

    def test_exact_result_host_identifier_and_digest_are_required(self):
        transport = object.__new__(agent.Transport)
        transport.host = 'pi-maintenance'
        valid = {'accepted': True, 'host': transport.host, 'id': 'one',
                 'result_sha256': agent.digest(RESULT)}
        for field, value in [('accepted', False), ('host', 'mac-maintenance'),
                             ('id', 'other'), ('result_sha256', 'wrong')]:
            transport.exchange = lambda body, changed=dict(valid, **{field: value}): changed
            with self.assertRaises(ValueError):
                transport.complete('one', RESULT)
        transport.exchange = lambda body: valid
        self.assertEqual(transport.complete('one', RESULT), agent.digest(RESULT))



def wait_then_execute(home, incoming, outgoing, gate):
    gate.wait()
    agent.executor_main(home, incoming, outgoing)


class ProcessIntegrationTests(unittest.TestCase):
    def start_worker(self, home, target=agent.executor_main, extra=()):
        context = agent.multiprocessing.get_context('fork')
        incoming, outgoing = context.Queue(maxsize=1), context.Queue()
        worker = context.Process(target=target, args=(home, incoming, outgoing) + extra)
        worker.daemon = True
        worker.start()
        def cleanup():
            worker.terminate()
            worker.join(5)
            incoming.cancel_join_thread()
            outgoing.cancel_join_thread()
            incoming.close()
            outgoing.close()
        self.addCleanup(cleanup)
        return worker, incoming, outgoing

    def test_real_child_storage_lock_cannot_block_diagnostic_heartbeat(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        context = agent.multiprocessing.get_context('fork')
        gate = context.Event()
        worker, incoming, outgoing = self.start_worker(
            folder.name, wait_then_execute, (gate,))
        # Acquire after fork so the child never inherits this SQLite connection.
        lock = sqlite3.connect(os.path.join(folder.name, 'ledger.sqlite'))
        self.addCleanup(lock.close)
        lock.execute('BEGIN EXCLUSIVE')
        gate.set()
        transport = FakeTransport()
        client = agent.Agent(transport, incoming, outgoing, worker)
        client.step({'version': 1, 'agent': agent.AGENT})
        self.assertEqual([x['operation'] for x in transport.calls], ['heartbeat'])
        self.assertTrue(incoming.empty())
        lock.rollback()

    def test_real_process_admission_result_ack_and_redelivery_execute_once(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        worker, incoming, outgoing = self.start_worker(folder.name)
        transport = FakeTransport()
        counter = os.path.join(folder.name, 'execution-counter')
        transport.jobs = [{'id': 'one', 'argv': [
            '/bin/sh', '-c', 'printf x >> "$1"; printf done', 'sh', counter], 'cwd': None}]
        client = agent.Agent(transport, incoming, outgoing, worker)
        deadline, reported = time.monotonic() + 10, False
        while time.monotonic() < deadline:
            client.step({'version': 1, 'agent': agent.AGENT})
            self.assertFalse(client.failed)
            if any(item['operation'] == 'complete' for item in transport.calls):
                with sqlite3.connect(os.path.join(folder.name, 'ledger.sqlite')) as database:
                    row = database.execute('SELECT state FROM commands WHERE id=?', ('one',)).fetchone()
                if row and row[0] == 'reported':
                    reported = True
                    break
            time.sleep(0.01)
        self.assertTrue(reported, 'real child never completed its durable ACK path')
        # Continue presenting the same gateway command after it was completed.
        for unused in range(5):
            client.step({'version': 1, 'agent': agent.AGENT})
            time.sleep(0.01)
        self.assertEqual(agent.read_text(counter), 'x')
        completed = [item for item in transport.calls if item['operation'] == 'complete']
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]['result']['stdout'], 'done')

class TLSAndInstallationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('openssl'):
            raise unittest.SkipTest('openssl is needed for real certificate tests')
        cls.folder = tempfile.TemporaryDirectory()
        cls.cert = os.path.join(cls.folder.name, 'certificate.pem')
        cls.key = os.path.join(cls.folder.name, 'private.pem')
        subprocess.check_call(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                               '-nodes', '-keyout', cls.key, '-out', cls.cert,
                               '-days', '1', '-subj', '/CN=localhost'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path.endswith('/redirect'):
                    self.send_response(302)
                    self.send_header('Location', 'https://other.invalid/maintenance/device')
                    self.end_headers()
                    return
                response = json.dumps({'accepted': True, 'host': 'pi-test'}).encode('ascii')
                self.send_response(200)
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            def log_message(self, *args):
                pass
        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        context.load_cert_chain(cls.cert, cls.key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.folder.cleanup()

    def transport(self, hostname='localhost', trusted=True):
        return agent.Transport('https://%s:%d' % (hostname, self.server.server_port),
                               'a' * 64, 'pi-test',
                               cafile=self.cert if trusted else None, timeout=3)

    def test_verified_tls_succeeds_with_trusted_matching_hostname(self):
        self.assertEqual(self.transport().exchange({'operation': 'heartbeat'})['host'], 'pi-test')

    def test_untrusted_certificate_is_rejected(self):
        with self.assertRaises((URLError, ssl.SSLError, ssl.CertificateError)):
            self.transport(trusted=False).exchange({'operation': 'heartbeat'})

    def test_trusted_certificate_with_wrong_hostname_is_rejected(self):
        with self.assertRaises((URLError, ssl.SSLError, ssl.CertificateError)):
            self.transport(hostname='127.0.0.1').exchange({'operation': 'heartbeat'})

    def test_redirect_cannot_forward_the_device_credential(self):
        transport = self.transport()
        transport.url += '/redirect'
        with self.assertRaises(HTTPError) as caught:
            transport.exchange({'operation': 'heartbeat'})
        self.assertEqual(caught.exception.code, 302)

    def test_insecure_or_credential_bearing_origin_is_rejected(self):
        for origin in ['http://localhost', 'https://name:secret@localhost',
                       'https://localhost/path', 'https://localhost?token=value']:
            with self.assertRaises(ValueError):
                agent.Transport(origin, 'a' * 64, 'pi-test')


class PreparationTests(unittest.TestCase):
    def test_preparation_printable_result_contains_only_verifier_and_does_not_activate(self):
        with tempfile.TemporaryDirectory() as folder:
            home = os.path.join(folder, 'new-home')
            with mock.patch.object(agent, 'require_durable_home', return_value='ext4'):
                result = installer.prepare(home, 'https://example.invalid', 'pi-test',
                                           '/usr/bin/python3', source='print("test")\n')
                again = installer.prepare(home, 'https://example.invalid', 'pi-test',
                                          '/usr/bin/python3', source='print("test")\n')
            key = agent.private_file(os.path.join(home, 'device.key')).strip()
            self.assertEqual(len(key), 64)
            self.assertEqual(result['key_hash'], hashlib.sha256(key.encode('ascii')).hexdigest())
            self.assertNotIn(key, json.dumps(result))
            self.assertEqual(result, again)
            self.assertFalse(result['activated'])
            self.assertEqual(os.stat(os.path.join(home, 'device.key')).st_mode & 0o777, 0o600)
            with mock.patch.object(agent, 'require_durable_home', return_value='ext4'):
                with self.assertRaises(ValueError):
                    installer.prepare(home, 'https://different.invalid', 'pi-test',
                                      '/usr/bin/python3', source='print("test")\n')
            self.assertEqual(agent.private_file(os.path.join(home, 'device.key')).strip(), key)

    def test_unit_has_no_controller_dependency_or_controller_restart(self):
        text = installer.unit_text('/home/pi/private', 'pi', '/usr/bin/python3')
        self.assertIn('RuntimeDirectory=biomerte-maintenance', text)
        self.assertIn('StandardOutput=null', text)
        self.assertIn('StandardError=null', text)
        self.assertIn('User=pi', text)
        self.assertNotIn('biomerte.controller', text)
        self.assertNotIn('systemctl', text)
        self.assertNotIn('launchctl', text)
        self.assertNotIn('ExecStartPre', text)
        self.assertIn('"/home/pi/private"', text)

    def test_tmpfs_and_network_filesystems_cannot_hold_replay_authority(self):
        for filesystem in ['tmpfs', 'ramfs', 'nfs', 'nfs4', 'cifs', 'fuse.sshfs', None]:
            with mock.patch.object(agent, 'filesystem_type', return_value=filesystem):
                with self.assertRaises(ValueError):
                    agent.require_durable_home('/run/biomerte-maintenance')
        with mock.patch.object(agent, 'filesystem_type', return_value='ext4'):
            self.assertEqual(agent.require_durable_home('/home/pi/private'), 'ext4')

    def test_mount_selection_uses_most_specific_mount_and_decodes_spaces(self):
        with tempfile.TemporaryDirectory() as folder:
            mountinfo = os.path.join(folder, 'mountinfo')
            with open(mountinfo, 'w') as stream:
                stream.write('1 0 1:1 / / rw - ext4 /dev/mmcblk0 rw\n')
                stream.write('2 1 0:2 / /run rw - tmpfs tmpfs rw\n')
                stream.write('3 1 8:1 / /disk\\040one rw - ext4 /dev/sda1 rw\n')
            self.assertEqual(agent.filesystem_type('/run/state', mountinfo), 'tmpfs')
            self.assertEqual(agent.filesystem_type('/disk one/state', mountinfo), 'ext4')
            self.assertEqual(agent.filesystem_type('/home/pi/state', mountinfo), 'ext4')


if __name__ == '__main__':
    unittest.main()
