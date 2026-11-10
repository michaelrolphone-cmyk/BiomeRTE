"""Native barrier ordering and immutable capture without a real engine."""
import copy
import sys
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import async_checkpoint


class NativeCaptureTests(unittest.TestCase):
    def test_snapshot_precedes_save_barrier_and_completion_wait_is_off_thread(self):
        events = []
        graph = {"keyframes":[{"id":1, "position":[0,0,0]}]}
        process = SimpleNamespace(is_running=SimpleNamespace(value=1),
            save_request_completed=SimpleNamespace(value=1),
            save_request_condition=threading.Condition())
        process.add_task = lambda task: events.append(("save", task.path))
        module = SimpleNamespace(
            Parameters=SimpleNamespace(USE_CPP_CORE=False),
            SerializationJSON=SimpleNamespace(serialize=copy.deepcopy),
            format_floats_for_json=lambda x:x,
            LoopDetectorTaskType=SimpleNamespace(SAVE="save"),
            LoopDetectorTask=lambda *args, **kwargs:SimpleNamespace(path=kwargs["load_save_path"]))
        slam = SimpleNamespace(volumetric_integrator=None,
            map=SimpleNamespace(to_json=lambda:(events.append(("map",None)), graph)[1]),
            sensor_type="monocular", environment_type="indoor",
            feature_tracker_config={}, loop_detector_config={}, semantic_mapping_config={},
            loop_closing=SimpleNamespace(headless=True, loop_detecting_process=process))
        with tempfile.TemporaryDirectory() as root, patch.object(
                async_checkpoint.importlib, "import_module", return_value=module):
            ticket = async_checkpoint.capture(slam, root)
            self.assertEqual([e[0] for e in events], ["map","save"])
            self.assertEqual(process.save_request_completed.value, 0)
            graph["keyframes"][0]["position"][0] = 99
            self.assertEqual(ticket["document"]["map"]["keyframes"][0]["position"], [0,0,0])
            done = threading.Event()
            writer = threading.Thread(target=lambda:(async_checkpoint.write(ticket),done.set()))
            writer.start()
            try:
                self.assertFalse(done.wait(.05))
                with process.save_request_condition:
                    process.save_request_completed.value = 1
                    process.save_request_condition.notify_all()
                self.assertTrue(done.wait(1))
            finally:
                writer.join(2)
            self.assertTrue((Path(root)/"map.json").is_file())


if __name__ == "__main__":
    unittest.main()
