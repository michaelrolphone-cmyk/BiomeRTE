import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from biome_provider import stream_offer


class StreamReadinessTests(unittest.TestCase):
    config = {"provider": "camera", "resource": "rover", "slots": 1}

    def test_live_lane_survives_mapper_transitions(self):
        for state in ("running", "switching_map", "switching_submap", "backend_start_failed"):
            with self.subTest(state=state):
                status = {"state": state, "streaming": {"available": True, "lanes": ["vision", "map"]}}
                self.assertEqual(stream_offer(self.config, status)["state"], "ready")

    def test_unavailable_incomplete_or_stopping_endpoint_withdraws_offer(self):
        for status in ({}, {"state": "running", "streaming": {"available": False, "lanes": ["vision", "map"]}},
                       {"state": "running", "streaming": {"available": True, "lanes": ["map"]}},
                       {"state": "stopping", "streaming": {"available": True, "lanes": ["vision", "map"]}}):
            self.assertEqual(stream_offer(self.config, status)["state"], "offline")
