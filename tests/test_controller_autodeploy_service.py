"""Native restart selection tests; never manipulate real services."""
import os
import tempfile
import unittest

from tests.test_controller_autodeploy import ROOT, load_module

service = load_module('controller_autodeploy_service',
    os.path.join(ROOT, 'deploy', 'controller_autodeploy_service.py'))


class NativeRestartTests(unittest.TestCase):
    def setUp(self):
        descriptor, self.plist = tempfile.mkstemp(suffix='.plist')
        os.close(descriptor)
        self.config = {'service': {'manager': 'launchd',
            'label': 'org.biomerte.controller', 'domain': 'gui/501',
            'plist': self.plist}, 'command_timeout': 17}
        self.calls = []

    def tearDown(self):
        os.unlink(self.plist)

    def runner(self, loaded):
        def invoke(argv, timeout, check=True):
            self.calls.append((argv, timeout, check))
            return (0 if loaded else 113) if argv[1] == 'print' else 0
        return invoke

    def test_missing_launchd_service_is_bootstrapped_then_kickstarted(self):
        service.restart(self.config, runner=self.runner(False))
        self.assertEqual([call[0] for call in self.calls], [
            ['/bin/launchctl', 'print', 'gui/501/org.biomerte.controller'],
            ['/bin/launchctl', 'bootstrap', 'gui/501', self.plist],
            ['/bin/launchctl', 'kickstart', '-k', 'gui/501/org.biomerte.controller']])
        self.assertFalse(self.calls[0][2])
        self.assertTrue(all(call[1] == 17 for call in self.calls))

    def test_loaded_launchd_service_only_kickstarts_without_reload(self):
        service.restart(self.config, runner=self.runner(True))
        self.assertEqual([call[0][1] for call in self.calls], ['print', 'kickstart'])

    def test_first_adoption_reloads_exact_saved_plist(self):
        self.config['service']['reload_on_restart'] = True
        service.restart(self.config, runner=self.runner(True))
        self.assertEqual([call[0] for call in self.calls], [
            ['/bin/launchctl', 'print', 'gui/501/org.biomerte.controller'],
            ['/bin/launchctl', 'bootout', 'gui/501/org.biomerte.controller'],
            ['/bin/launchctl', 'bootstrap', 'gui/501', self.plist],
            ['/bin/launchctl', 'kickstart', '-k', 'gui/501/org.biomerte.controller']])

    def test_pi_reload_and_restart_are_scoped_to_one_unit(self):
        self.config['service'] = {'manager': 'systemd', 'unit': 'biomerte.service',
                                  'sudo': True}
        service.restart(self.config, runner=self.runner(True))
        self.assertEqual([call[0] for call in self.calls], [
            ['/usr/bin/sudo', '-n', '/bin/systemctl', 'daemon-reload'],
            ['/usr/bin/sudo', '-n', '/bin/systemctl', 'restart', 'biomerte.service']])

    def test_invalid_unit_does_not_run_a_command(self):
        self.config['service'] = {'manager': 'systemd', 'unit': '*.service'}
        with self.assertRaises(service.ServiceError):
            service.restart(self.config, runner=self.runner(True))
        self.assertEqual(self.calls, [])

    def test_bootstrap_failure_is_reported_and_does_not_claim_restart(self):
        def fail_bootstrap(argv, timeout, check=True):
            self.calls.append(argv)
            if argv[1] == 'print':
                return 113
            raise service.ServiceError('native_service_exit_5')
        with self.assertRaises(service.ServiceError):
            service.restart(self.config, runner=fail_bootstrap)
        self.assertEqual([call[1] for call in self.calls], ['print', 'bootstrap'])


if __name__ == '__main__':
    unittest.main()
