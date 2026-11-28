"""Cyclic finalization may change the idle pool during exclusive checkout."""
import gc
import sqlite3
import sys
import tempfile
import unittest
from biomerte.store import Store


class CheckoutCollection(unittest.TestCase):
    def test_finalizer_during_checkout_cannot_invalidate_pool_iteration(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, 'kept')
            enabled = gc.isenabled()
            previous_trace = sys.gettrace()
            gc.disable()
            try:
                with store.connection() as expected:
                    pass
                other = Store(root, 'cyclic')
                other.cycle = [other]
                with other.connection() as retired:
                    pass
                del other
                collected = []
                def collect(frame, event, arg):
                    if (not collected and event == 'line' and frame.f_code.co_name == 'connection' and
                            frame.f_locals.get('self') is store and
                            ('index' in frame.f_locals or 'entry' in frame.f_locals)):
                        collected.append(True)
                        gc.collect()
                    return collect
                sys.settrace(collect)
                with store.connection() as actual:
                    self.assertIs(actual, expected)
                    self.assertEqual(actual.execute('SELECT 1').fetchone(), (1,))
                self.assertTrue(collected)
                with self.assertRaises(sqlite3.ProgrammingError):
                    retired.execute('SELECT 1')
            finally:
                sys.settrace(previous_trace)
                if enabled:
                    gc.enable()
                store.close()


if __name__ == '__main__':
    unittest.main()
