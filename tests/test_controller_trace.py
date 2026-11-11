import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.api import OPERATIONS
from biomerte.store import Store

root = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('controller_trace', str(root/'deploy/controller_trace.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ControllerTraceTests(unittest.TestCase):
    def test_trace_preserves_reads_writes_and_excludes_all_argument_values(self):
        with tempfile.TemporaryDirectory() as home:
            initialize(home)
            app = Application(home)
            path = os.path.join(home,'trace.log')
            trace = module.RequestTrace(path, slow_seconds=0)
            restore = module.install(trace, Application, Store, OPERATIONS)
            try:
                result = app.call('knowledge.put', {'key':'diagnostic-probe','body':{'private':'secret-value'}},'trace-test')
                self.assertIn('rev', result)
                self.assertEqual(app.call('system.info')['shared_backend'],'sqlite')
                with trace.operation('system.info'), trace.phase('sqlite.connect'):
                    trace.sample()
            finally:
                restore()
                trace.close()
            raw = pathlib.Path(path).read_text()
            self.assertNotIn('secret-value',raw)
            self.assertNotIn('diagnostic-probe',raw)
            values = [json.loads(line) for line in raw.splitlines()]
            self.assertTrue(any(x['event']=='completed_slow' for x in values))
            self.assertTrue(any(x.get('phase')=='sqlite.connect' for x in values))
            self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            self.assertEqual(trace.active,{})

    def test_exception_restores_trace_state_and_keeps_original_error(self):
        with tempfile.TemporaryDirectory() as home:
            trace = module.RequestTrace(os.path.join(home,'trace.log'),slow_seconds=0)
            with self.assertRaisesRegex(ValueError,'private-error'):
                with trace.operation('system.info'), trace.phase('operation'):
                    raise ValueError('private-error')
            trace.close()
            self.assertEqual(trace.active,{})
            self.assertNotIn('private-error',pathlib.Path(home,'trace.log').read_text())

    def test_rotation_retains_only_two_bounded_files(self):
        with tempfile.TemporaryDirectory() as home:
            trace = module.RequestTrace(os.path.join(home,'trace.log'))
            trace.handler.maxBytes = 256
            for unused in range(40):
                trace.emit({'event':'fixture','seconds':1,'phase':'sqlite.connect'})
            trace.close()
            self.assertEqual(sorted(os.listdir(home)),['trace.log','trace.log.1'])
            self.assertTrue(all(os.stat(os.path.join(home,n)).st_size<256 for n in os.listdir(home)))
            self.assertTrue(all(os.stat(os.path.join(home,n)).st_mode & 0o777 == 0o600 for n in os.listdir(home)))

    def test_full_diagnostic_queue_never_blocks_callers(self):
        with tempfile.TemporaryDirectory() as home:
            trace = module.RequestTrace(os.path.join(home,'trace.log'))
            with patch.object(trace.logger, 'warning', side_effect=AssertionError('Request path performed IO')):
                for unused in range(500):
                    trace.emit({'event':'fixture'})
            self.assertEqual(trace.records.qsize(),64)
            trace.close()
