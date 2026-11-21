import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from bridge_config import replace_if_hash, sha, is_disabled
from prepare import render


class HandoffPreparationTests(unittest.TestCase):
    def test_native_launchd_word_and_boolean_state(self):
        label = "org.biomerte.tof-coordinator"
        for value in ("true", "disabled"):
            self.assertTrue(is_disabled(f'"{label}" => {value}', label))
        for value in ("false", "enabled"):
            self.assertFalse(is_disabled(f'"{label}" => {value}', label))
        self.assertFalse(is_disabled(f'"{label}.other" => disabled', label))
        with self.assertRaises(RuntimeError):
            is_disabled(f'"{label}" => unknown', label)

    def test_concurrent_bridge_edit_is_preserved(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);current=root/"current.json";proposal=root/"proposal.json"
            original=b'{"anchors":{"enabled":false}}'
            current.write_bytes(original);proposal.write_text('{"anchors":{"enabled":true}}')
            current.write_text('{"anchors":{"enabled":false},"another_worker":true}')
            observed=current.read_bytes()
            with self.assertRaises(RuntimeError):replace_if_hash(current,proposal,sha(original))
            self.assertEqual(current.read_bytes(),observed)

    def test_apply_and_restore_exact_bridge_configuration(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);current=root/"current.json";before=root/"before.json";after=root/"after.json"
            before.write_text('{"anchors":{"enabled":false},"existing":"preserved"}')
            after.write_text('{"anchors":{"enabled":true},"existing":"preserved"}')
            current.write_bytes(before.read_bytes())
            result=replace_if_hash(current,after,sha(before.read_bytes()))
            self.assertEqual(json.loads(current.read_text())["existing"],"preserved")
            replace_if_hash(current,before,result)
            self.assertEqual(current.read_bytes(),before.read_bytes())

    def test_compose_uses_actual_entrypoint_state_and_private_loopback_ports(self):
        template=json.loads((ROOT/"compose.template.json").read_text())
        values=dict(IMAGE="sha256:"+"a"*64,USER="501:20",TOF_STATE="/native/tof",
                    VISION_STATE="/native/vision",CONFIG="/deployment/config",PRIVATE="/deployment/private",
                    CAMERA_ID="registered-front",HEALTH_PORT=18771,SURVEY_PORT=18772)
        actual=render(template,values)["services"]["tof"]
        self.assertNotIn("--prepare-only",actual["command"])
        self.assertEqual(actual["platform"],"linux/arm64")
        self.assertEqual(actual["image"],values["IMAGE"])
        self.assertTrue(all(x.startswith("127.0.0.1:") for x in actual["ports"]))
        self.assertEqual(actual["volumes"][0]["source"],"/native/tof")
        self.assertEqual(actual["volumes"][1]["source"],"/native/vision")
        self.assertIn("/readyz",actual["healthcheck"]["test"][-1])
        self.assertNotIn("privileged",actual)


if __name__=="__main__":unittest.main()
