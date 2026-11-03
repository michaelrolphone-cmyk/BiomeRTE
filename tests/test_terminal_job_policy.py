"""Regressions for opt-in terminal limits on personal compute providers."""
import importlib.util
import json
from pathlib import Path
import resource
import tempfile
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / 'providers/pi-terminal/terminal_limits.py'
spec = importlib.util.spec_from_file_location('terminal_limits_test', str(path))
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class TerminalJobPolicy(unittest.TestCase):
    def test_no_policy_and_live_changes(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / 'terminal-limits.json'
            self.assertEqual(policy.read_limits(home), {})
            with patch.object(resource, 'setrlimit') as set_limit:
                policy.apply_limits(policy.read_limits(home))
                set_limit.assert_not_called()
            for cpu in (7200, 14400):
                target.write_text(json.dumps({'cpu_seconds': cpu}))
                self.assertEqual(policy.read_limits(home), {'cpu_seconds': cpu})

    def test_preserves_tighter_inherited_limit(self):
        with patch.object(resource, 'getrlimit', return_value=(100, 200)), \
                patch.object(resource, 'setrlimit') as set_limit:
            policy.apply_limits({'memory_bytes': 250})
        set_limit.assert_called_once_with(resource.RLIMIT_AS, (100, 100))

    def test_invalid_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / 'terminal-limits.json'
            for invalid in ([], {'typo': 1}, {'duration_seconds': True}, {'file_bytes': 0}):
                target.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    policy.read_limits(home)
