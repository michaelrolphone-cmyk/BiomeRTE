import tempfile
import unittest
from types import SimpleNamespace
from biomerte.store import Store, digest, encode
from biomerte.process import _compact_assistant_temporaries
from biomerte.web import background


class RecoveryTests(unittest.TestCase):
    def test_large_transaction_round_trips_without_software_ceiling(self):
        class Journal:
            head = None
            entries = {}
            def read(self, key):
                return (self.head if key == 'head' else self.entries[key], str(self.head['seq']) if self.head else '0')
            def append(self, expected, entry):
                self.entries['log/%020d' % entry['seq']] = entry
                self.head = {'seq': entry['seq'], 'hash': digest(entry)}
                return self.head
        with tempfile.TemporaryDirectory() as directory:
            remote = Journal()
            store = Store(directory, 'a', remote)
            value = 'x' * 1600000
            store.execute(lambda tx: tx.put('test', 'large', value))
            self.assertGreater(len(encode(remote.entries['log/00000000000000000001'])), 900000)
            mirror = Store(directory, 'b', remote)
            self.assertEqual(mirror.execute(lambda tx: tx.need('test', 'large')), value)

    def test_compaction_preserves_error_branch_and_future_inputs(self):
        nodes = {
            'before': {'type': 'service', 'args': {'frame': {'$': 'frame'}}, 'save': 'prepared', 'next': 'map', 'on_error': 'fault'},
            'map': {'type': 'set', 'values': {'frame': {'$': 'prepared.frame'}}, 'next': 'end'},
            'end': {'type': 'end', 'result': {'$': 'frame'}},
            'fault': {'type': 'end', 'result': {'$': 'step'}},
        }
        run = {'name': 'assistant.test', 'tokens': [{'at': 'before', 'vars': {'frame': 1, 'step': 2, 'prepared': 'obsolete', 'route': 'obsolete'}}]}
        self.assertTrue(_compact_assistant_temporaries(run, {'graph': {'nodes': nodes}}))
        self.assertEqual(run['tokens'][0]['vars'], {'frame': 1, 'step': 2})

    def test_work_fault_is_separate_from_store_health(self):
        class Stop:
            done = False
            def is_set(self): return self.done
            def wait(self, unused): self.done = True
        for unavailable in (False, True):
            def execute(unused):
                if unavailable: raise OSError('store unavailable')
            def tick(unused): raise ValueError('workflow fault')
            app = SimpleNamespace(health={}, stores={'site': SimpleNamespace(execute=execute)}, tick=tick)
            with self.assertLogs('biomerte', level='WARNING'):
                background(app, Stop(), 'site')
            self.assertEqual(app.health['site'] == 'ready', not unavailable)
            self.assertEqual(app.background_errors['site'], 'workflow fault')
