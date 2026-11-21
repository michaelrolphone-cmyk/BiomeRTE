"""Exercise the real provider dispatch against a fixture native boundary."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/"providers/robot-vision")]
spec = importlib.util.spec_from_file_location("packaged_vision_provider",
        ROOT/"providers/robot-vision/biome_provider.py")
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)

class Remote:
    def call(self, *args, **kwargs):
        raise AssertionError("qualification cannot contact the live gateway")

class Native:
    def __init__(self):
        self.calls = []
    def vision_query(self, operation, inputs):
        self.calls.append((operation, inputs))
        if operation == "robot.vision.status":
            return {"state":"ready", "backend":"fixture-native"}
        if operation == "robot.video.import.read":
            return {"ok":True, "jobs":[]}
        raise AssertionError(operation)

native = Native()
config = {"state":"/fixture-state", "socket":"/unmounted/native.sock",
          "host_io_config":"/fixture-host-io.json"}
with patch("biomerte.host_io_client.HostIOClient.from_config", return_value=native):
    result = provider.execute({"contract":"robot.vision.status:1","inputs":{}}, config, Remote())
    assert result["result"]["backend"] == "fixture-native"
    result = provider.execute({"contract":"robot.video.import.read:1",
                               "inputs":{"request":{"action":"list"}}}, config, Remote())
    assert result["result"]["ok"] is True
assert [item[0] for item in native.calls] == ["robot.vision.status","robot.video.import.read"]
assert "robot.vision.rig.configure:1" in provider.CONTRACTS
assert "robot.video.import.write:1" in provider.MUTATING_CONTRACTS
from leased_streams import VisionStreams
from stream_sequences import StreamSequences
print(json.dumps({"qualified":"actual-vision-provider-dispatch",
                  "contracts":len(provider.CONTRACTS), "native_calls":len(native.calls),
                  "live_gateway_calls":0, "compute_device_changed":False}))

import unittest
result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(Path(__file__).parent),pattern="test_boundary.py"))
if not result.wasSuccessful():
    raise SystemExit(1)
