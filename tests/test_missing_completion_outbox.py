import os
import unittest
from biomerte import supervised_commands as supervised
from biomerte import command_runner as runner
from biomerte.store import Fault
from tests import test_supervised_commands as fixtures


class MissingCompletionOutbox(unittest.TestCase):
    setUp = fixtures.RunnerTests.setUp
    worker = fixtures.RunnerTests.worker
    argv = fixtures.RunnerTests.argv
    action = fixtures.RunnerTests.action
    text = fixtures.RunnerTests.text
    def test_missing_authoritative_receipt_parks_outbox_without_ack_or_replay(self):
        class MissingRemote(fixtures.Remote):
            def call(self, name, args, rid=None):
                if name == 'provider.result':
                    raise Fault('No retained authoritative completion', 404)
                if name == 'cap.status':
                    return dict(id=args['operation'], state='missing_unknown', acknowledged=False,
                                result_expired=True, replay_allowed=False)
                return fixtures.Remote.call(self, name, args, rid)
        worker, _ = self.worker(MissingRemote())
        try:
            worker.perform(self.action())
            self.assertEqual(supervised.pending(worker), [])
            parked = supervised.unresolved(worker)
            self.assertEqual(len(parked), 1)
            self.assertFalse(parked[0]['acknowledged'])
            directory = runner.location(os.path.join(self.root, 'command-runners'), 'command')
            self.assertTrue(os.path.exists(os.path.join(directory, 'stdout')))
            self.assertFalse(os.path.exists(os.path.join(directory, 'acknowledged.json')))
            # An explicit reconciliation attempt still cannot launch again.
            worker.perform(self.action())
            self.assertEqual(self.text(os.path.join(self.root, 'effects')), 'once\n')
            self.assertEqual(list(worker.db.execute('SELECT id FROM supervised_delivery')), [])
        finally:
            worker.db.close()


if __name__ == '__main__':
    unittest.main()
