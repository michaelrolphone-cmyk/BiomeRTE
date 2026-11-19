import fcntl
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.test_controller_watchdog import watchdog, observation


class RecoveryDeploymentLockTests(unittest.TestCase):
    def test_deployment_lock_defers_restart_without_claiming_execution(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'recovery.lock')
            with open(path, 'w') as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                recovery = watchdog.Recovery({'failure_threshold': 1})
                with patch.object(subprocess, 'run') as run:
                    state = recovery.observe(observation(local=False),
                        lambda service: watchdog.restart_service(service, path), now=1)
                self.assertEqual(run.call_count, 0)
                self.assertEqual(state['status'], 'deferred')
                self.assertEqual(state['last_action']['outcome'], 'deferred')
                self.assertEqual(state['restarts']['controller'], 0)
            with patch.object(subprocess, 'run') as run:
                state = recovery.observe(observation(local=False),
                    lambda service: watchdog.restart_service(service, path), now=2)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(state['status'], 'recovering')
            self.assertEqual(state['last_action']['outcome'], 'restart_issued')
            self.assertEqual(state['restarts']['controller'], 1)


if __name__ == '__main__':
    unittest.main()
