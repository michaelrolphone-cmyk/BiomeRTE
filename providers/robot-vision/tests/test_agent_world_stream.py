"""The world bootstrap and frame extensions are observable by a passive subscriber."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from leased_maps import LiveMap
from stream_telemetry import bounded_metadata
from world_snapshot_stream import WorldSnapshotFeed
from biomerte.stream_router import decode


class AgentWorldStreamTest(unittest.TestCase):
    def test_attach_then_revision_with_all_pinned_pages(self):
        service = Mock()
        service.live_maps = LiveMap(world=True)
        feed = WorldSnapshotFeed(service)
        rev = [4]
        feed.version = lambda: ("world", rev[0], None)

        def read(op, args):
            self.assertEqual(op, "robot.world.read")
            tail = bool(args.get("cursor"))
            return {"map":{"map_id":"world", "revision":rev[0]},
                    "entities":[{"id":2 if tail else 1}], "submaps":[],
                    "unlocalized_submaps":[], "next_cursor":None if tail else "pinned"}

        service.query.side_effect = read
        with service.live_maps.subscribe() as early:
            service.live_maps.publish("local", 0, 1, [])
            self.assertTrue(early.empty())
            feed.refresh()
            with service.live_maps.subscribe() as late:
                first = early.get_nowait()
                self.assertEqual(late.get_nowait(), first)
                m, _ = decode(first[4:], None)
                self.assertEqual(m["data"]["entities"], [{"id":1},{"id":2}])
                self.assertIsNone(m["data"]["next_cursor"])
                feed.refresh()
                self.assertTrue(early.empty())
                self.assertTrue(late.empty())
                self.assertEqual(service.query.call_count, 2)
                rev[0] += 1
                feed.refresh()
                msg = early.get_nowait()
                self.assertEqual(msg, late.get_nowait())
                self.assertEqual(decode(msg[4:], None)[0]["revision"], 5)

    def test_preserves_computed_values_and_marks_missing_fields(self):
        value = {"frame_id":"a", "tof":{"baseline":{"epoch":3,"revision":8,"frame_id":"enu"},
                 "registration":{"valid":False,"sample_count":7}}, "motion_events":{"events":[]}}
        wire = bounded_metadata(value)
        r = wire["tof"]["registration"]
        self.assertEqual([r["frame_id"],r["baseline_epoch"],r["baseline_revision"]],["enu",3,8])
        self.assertEqual(r["sample_count"],7)
        self.assertIsNone(r["current_camera"])
        self.assertIsNone(r["T_anchor_map"])
        self.assertIn("range_exclusions", wire["tof"]["rover_refinement"])
        self.assertEqual(wire["event_delivery"]["recent_events_omitted"],0)
        self.assertNotIn("baseline_epoch",value["tof"]["registration"])
        value["event_delivery"]={"recent_events_omitted":5}
        value["tof"]["registration"]["current_camera"]={"position_anchor_m":[1,2,3]}
        other = bounded_metadata(value)
        self.assertEqual(other["event_delivery"]["recent_events_omitted"],5)
        self.assertEqual(other["tof"]["registration"]["current_camera"]["position_anchor_m"],[1,2,3])
