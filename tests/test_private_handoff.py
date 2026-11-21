"""Actual launchctl word-state compatibility for private owner rollback."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1]/"deploy/workers/private/prepare.py"
spec = importlib.util.spec_from_file_location("private_handoff_prepare",path)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

class LaunchdStateTests(unittest.TestCase):
    def test_native_word_states(self):
        text = 'disabled services = {\n "org.test.chat" => enabled\n "org.test.image" => disabled\n}'
        self.assertEqual(prepare.disabled_override(text,"org.test.chat"),"enabled")
        self.assertEqual(prepare.disabled_override(text,"org.test.image"),"disabled")
    def test_boolean_states_remain_compatible(self):
        text = '"org.test.chat" => false\n "org.test.image" => true\n'
        self.assertEqual(prepare.disabled_override(text,"org.test.chat"),"false")
        self.assertEqual(prepare.disabled_override(text,"org.test.image"),"true")
    def test_no_override_is_distinct_from_explicit_disabled(self):
        self.assertIsNone(prepare.disabled_override('"org.test.other" => disabled',"org.test.chat"))
    def test_unknown_explicit_state_fails_closed(self):
        with self.assertRaises(ValueError):
            prepare.disabled_override('"org.test.chat" => unexpected',"org.test.chat")

if __name__ == "__main__":
    unittest.main()
