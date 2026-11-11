"""A reconciled controller outcome must repair a stale supervised outbox."""
import os
import sys
import tempfile
import unittest
from tests import test_supervised_commands as fixtures
from tests.test_supervised_commands import Remote
from biomerte import supervised_commands as supervised
from biomerte.store import Fault


class AuthoritativeRecovery(unittest.TestCase):
    def test_signed_terminal_outcome_repairs_local_pending_without_replay(self):
        self.run_case(False)

    def test_expired_outcome_repairs_pending_without_publishing_summary(self):
        self.run_case(True)

    def run_case(self, expired):
        with tempfile.TemporaryDirectory() as root:
            case = fixtures.RunnerTests()
            case.root = root
            class ProtectedRemote(Remote):
                def call(self, name, args, rid=None):
                    prior = self.results.get(args.get('operation')) if isinstance(args, dict) else None
                    if name == 'provider.result' and prior and prior.get('state') in ('succeeded', 'failed'):
                        if args['state'] != prior['state'] or args['result'] != prior['result']:
                            raise Fault('Conflicting authoritative outcome', 409)
                        return prior
                    return Remote.call(self, name, args, rid)
            remote = ProtectedRemote()
            worker, offer = case.worker(remote)
            marker = os.path.join(root, 'one-effect')
            offer['argv'] = [sys.executable, '-S', '-c',
                             "open(%r,'a').write('once\\n');raise SystemExit(1)" % marker]
            try:
                with self.assertRaises(Fault):
                    worker.perform(case.action())
                outcome = {'exit_code': 1, 'stdout': '', 'stderr': '', 'streams': {},
                           'evidence': {'quiescent': True, 'reason': 'exited'}}
                remote.results['command'] = dict(case.action(), state='failed', result=outcome)
                if expired:
                    from biomerte.worker import digest
                    a = case.action()
                    saved = remote.results['command']
                    saved.update(result_expired=True,
                                 identity_digest=digest([a[k] for k in ('contract','inputs','resource','generation','lease')]),
                                 result_digest=digest(outcome),
                                 recovery_result={'result_expired':True,'result_digest':digest(outcome),
                                                  'evidence':{'quiescent':True}})
                    del saved['result']
                worker.perform(case.action())
                self.assertEqual(supervised.pending(worker), [])
                self.assertEqual(remote.results['command']['state'], 'failed')
                with open(marker) as source:self.assertEqual(source.read(), 'once\n')
            finally:
                worker.db.close()


if __name__ == '__main__':
    unittest.main()
