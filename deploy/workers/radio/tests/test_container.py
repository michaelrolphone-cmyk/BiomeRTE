import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, BASE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class PackagingTests(unittest.TestCase):
    def test_rebind_preserves_identity_and_contracts(self):
        entry = load("entrypoint")
        old = {"url": "http://127.0.0.1:8740", "site": "existing-site", "timeout": None,
               "offers": [{"contract": "radio.sdr.receive:2", "resource": "radio.sdr.existing",
                           "capacity": 5, "configuration": {"host": "ProximityRF"},
                           "usage": {"before": "Existing ABI"}, "argv": ["/old/python", "/old/server.py"]}]}
        before = copy.deepcopy(old)
        new = entry.rewrite_manifest(old, "sdr", "/existing/home")
        expected = copy.deepcopy(before)
        expected["offers"][0]["argv"] = new["offers"][0]["argv"]
        self.assertEqual(new, expected)
        self.assertEqual(old, before)
        self.assertEqual(new["offers"][0]["argv"][-2:], ["call", "/existing/home"])
    def test_other_capability_cannot_be_started_by_radio_image(self):
        entry = load("entrypoint")
        with self.assertRaises(ValueError):
            entry.rewrite_manifest({"offers": [{"contract": "host.terminal.run:1", "resource": "host"}]}, "sdr", "/home")
    def test_fm_devices_and_privileges_are_explicit(self):
        launch = load("launch")
        c = {"kind": "fm", "image": "sha256:" + "a"*64, "hostname": "ProximityRF",
             "home": "/home/pi/.local/share/biomerte-fm", "state": "/home/pi/.local/share/fm-transmitter"}
        args = launch.argv(c)
        self.assertNotIn("--privileged", args)
        self.assertIn("/dev/mem:/dev/mem:rw", args)
        self.assertIn("/sys/firmware/devicetree/base:/proc/device-tree:ro", args)
        self.assertIn("/run/lock/capabilities-fm-transmit.lock:/run/lock/capabilities-fm-transmit.lock:rw", args)
        self.assertNotIn("--security-opt", args)  # sudo worker needs the declared setuid transition.
    def test_sdr_no_unrestricted_device_or_privilege_access(self):
        launch = load("launch")
        c = {"kind": "sdr", "image": "sha256:" + "b"*64, "hostname": "ProximityRF",
             "home": "/home/pi/.local/share/biomerte-sdr", "state": "/home/pi/.local/share/sdr-receiver",
             "devices": ["/dev/bus/usb/001/004", "/dev/ttyACM0"], "device_groups": [20, 46]}
        args = launch.argv(c)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--cap-add", args)
        self.assertIn("no-new-privileges", args)
        self.assertIn("/dev/ttyACM0:/dev/ttyACM0:rw", args)
        c["devices"] = ["/dev/mem"]
        with self.assertRaises(ValueError):
            launch.argv(c)
    def test_mutable_image_rejected(self):
        launch = load("launch")
        with self.assertRaises(ValueError):
            launch.validate({"kind": "sdr", "image": "example:latest"})
