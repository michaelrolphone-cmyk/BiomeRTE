import os
import tempfile
import unittest
from unittest import mock
from biomerte import command_runner as runner


class CompletionRace(unittest.TestCase):
    def test_completion_committing_during_inspection_is_not_lost_custody(self):
        with tempfile.TemporaryDirectory() as directory:
            runner.save(os.path.join(directory, 'state.json'), {'state':'running'})
            complete = {'state':'finished','returncode':0,'observed_at':1.0}
            original = runner.locked
            def commit_then_release(path):
                runner.save(os.path.join(directory, 'completion.json'), complete)
                return original(path)
            with mock.patch.object(runner, 'locked', side_effect=commit_then_release):
                self.assertEqual(runner.inspect(directory), complete)


if __name__ == '__main__':
    unittest.main()
