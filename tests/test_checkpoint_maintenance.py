import os
import sys
import unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
import checkpoint_maintenance as maintenance


class CheckpointMaintenanceTests(unittest.TestCase):
    def fixture(self, mode='bootstrap'):
        settings = {'controller_id': 'pi', 'checkpoint_mode': mode}
        status = {'primary': 'pi', 'ready': True, 'head': {'seq': 100000}}
        clock = [0.0]
        calls = []
        def send(path, body, timeout):
            calls.append((path, timeout))
            clock[0] += 0.2
            return {'advanced': True, 'seq': len(calls)}
        return settings, status, clock, calls, send

    def run_fixture(self, settings, status, clock, call):
        return maintenance.run_burst(settings, status, call, clock=lambda: clock[0],
                                     sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def test_default_off_and_standby_make_no_requests(self):
        settings, status, clock, calls, send = self.fixture()
        del settings['checkpoint_mode']
        self.assertEqual(self.run_fixture(settings, status, clock, send)['outcome'], 'disabled')
        settings['checkpoint_mode'] = 'bootstrap'
        status['primary'] = 'mac'
        self.assertEqual(self.run_fixture(settings, status, clock, send)['outcome'], 'standby')
        self.assertEqual(calls, [])

    def test_burst_page_limit_and_request_spacing(self):
        settings, status, clock, calls, send = self.fixture()
        result = self.run_fixture(settings, status, clock, send)
        self.assertEqual(result, {'pages': 32, 'outcome': 'yield'})
        self.assertTrue(all(path == 'ha/checkpoint/build' and timeout == maintenance.REQUEST_TIMEOUT for path, timeout in calls))
        self.assertAlmostEqual(clock[0], 32 * 0.3)

    def test_time_budget_and_role_loss_end_burst(self):
        settings, status, clock, calls, send = self.fixture()
        def slow(path, body, timeout):
            result = send(path, body, timeout)
            clock[0] += 9.8
            return result
        self.assertLessEqual(self.run_fixture(settings, status, clock, slow)['pages'], 3)
        settings, status, clock, calls, send = self.fixture()
        def switched(path, body, timeout):
            result = send(path, body, timeout)
            status['primary'] = 'mac'
            return result
        self.assertEqual(self.run_fixture(settings, status, clock, switched), {'pages': 1, 'outcome': 'standby'})

    def test_slow_page_gets_full_timeout_near_admission_deadline(self):
        settings, status, clock, calls, send = self.fixture()
        def slow(path, body, timeout):
            calls.append((path, timeout))
            self.assertEqual(timeout, 120.0)
            # The second page starts with only 0.9 seconds in the admission
            # window, but must still have time to persist its complete cursor.
            clock[0] += 29.0 if len(calls) == 1 else timeout
            return {'advanced': True, 'seq': len(calls)}
        result = self.run_fixture(settings, status, clock, slow)
        self.assertEqual(result, {'pages': 2, 'outcome': 'yield'})
        self.assertAlmostEqual(clock[0], 149.1)
        self.assertLessEqual(clock[0], maintenance.BURST_SECONDS + maintenance.REQUEST_TIMEOUT)
        self.assertTrue(status['ready'])

    def test_timeout_ends_burst_without_retry_or_lease_action(self):
        settings, status, clock, calls, send = self.fixture()
        def timeout(path, body, timeout):
            calls.append((path, timeout))
            clock[0] += timeout
            raise TimeoutError('maintenance request timed out')
        result = self.run_fixture(settings, status, clock, timeout)
        self.assertEqual(result, {'pages': 0, 'outcome': 'error'})
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(clock[0], maintenance.BURST_SECONDS + maintenance.REQUEST_TIMEOUT)
        self.assertTrue(status['ready'])

    def test_maintenance_disabled_and_caught_up_stop(self):
        settings, status, clock, calls, send = self.fixture('maintain')
        def disabled(path, body, timeout):
            self.assertEqual(path, 'ha/checkpoint/maintain')
            return {'enabled': False}
        self.assertEqual(self.run_fixture(settings, status, clock, disabled)['outcome'], 'disabled')
        self.assertEqual(self.run_fixture(settings, status, clock, lambda *a, **k: {'enabled': True, 'checkpoint': {'advanced': False}, 'compacted': {'removed': 0}})['outcome'], 'caught_up')

    def test_busy_or_error_does_not_spin_or_touch_heartbeat(self):
        settings, status, clock, calls, send = self.fixture()
        self.assertEqual(self.run_fixture(settings, status, clock, lambda *a, **k: {'busy': True})['outcome'], 'busy')
        def fail(*args, **kwargs):
            raise RuntimeError('unavailable')
        self.assertEqual(self.run_fixture(settings, status, clock, fail)['outcome'], 'error')
        self.assertTrue(status['ready'])


if __name__ == '__main__':
    unittest.main()
