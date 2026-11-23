"""Complete world snapshots must not reconstruct every sonar ray per page."""
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multisensor_mapping import SonarObstacleMap
from world_atlas import WorldAtlas
from world_snapshot_stream import WorldSnapshotFeed, snapshot_message


class NativeStore:
    def read_map(self, map_id, revision=None, kinds=None, cursor=None, limit=500):
        rows = [
            {"kind": "sparse_point", "entity_id": "visual:" + str(i),
             "revision": revision, "data": {"xyz": [i, 0, 0]}}
            for i in range(7)
        ]
        offset = int(cursor or 0)
        return {"entities": rows[offset:offset+limit],
                "next_cursor": str(offset+limit) if offset+limit < len(rows) else None}


class SnapshotSonarWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sonar = SonarObstacleMap(self.root / "sonar.sqlite3", {
            "front": {"origin_camera_m": [0, 0, 0],
                      "direction_camera": [0, 0, 1], "beam_half_angle_deg": 7.5}})
        self.add_rays(0, 1203, revision=7)
        self.atlas = WorldAtlas(self.root / "world.sqlite3", NativeStore())
        self.transform = np.eye(4)
        self.transform[0, 3] = 10
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("UPDATE world SET root=?,revision=1", ("map",))
            self.atlas.db.execute("INSERT INTO submaps VALUES(?,?,?,?,?,?)",
                ("map", self.atlas.world_id, 7, json.dumps(self.transform.tolist()), None, None))
        self.reconstructions = []
        self.atlas.sonar_entities = self.sonar_entities
        self.messages = []
        self.service = SimpleNamespace(
            map_id="map", revision=7, atlas=self.atlas, state_dir=self.root,
            query=self.query,
            live_maps=SimpleNamespace(publish_world=self.messages.append,
                                      publish_local=lambda value: self.fail("unexpected local snapshot")))
        self.feed = WorldSnapshotFeed(self.service)
        self.feed.local_key = ("map", 7)

    def tearDown(self):
        self.atlas.close()
        self.sonar.close()
        self.temp.cleanup()

    def add_rays(self, first, count, revision):
        observations = [
            {"id": "ray:%05d" % i, "sensor_id": "front", "boot_id": "fixture",
             "time_s": float(i), "distance_m": 1 + i / 1000}
            for i in range(first, first+count)
        ]
        self.assertEqual(count, self.sonar.record(
            "map", "capture", observations, lambda *args: np.eye(4), revision=revision))

    def sonar_entities(self, map_id, revision):
        # Exercise the real SQLite reader, pose/mount validation and transforms.
        rays = self.sonar.read(map_id, scale_m_per_map_unit=1, revision=revision,
                               world_rays_only=True)["rays"]
        self.reconstructions.append((map_id, revision, len(rays)))
        return [{"kind": "sparse_point", "entity_id": ray["id"], "revision": revision,
                 "data": {"xyz": ray["endpoint_m"], "source": "sonar",
                          "sensor_id": ray["sensor_id"]}} for ray in rays]

    def query(self, operation, inputs):
        self.assertEqual(operation, "robot.world.read")
        return self.atlas.read(cursor=inputs.get("cursor"), limit=inputs.get("limit", 500))

    def decoded(self):
        value = self.messages[-1]
        self.assertEqual(struct.unpack("!I", value[:4])[0], len(value)-4)
        header_size = struct.unpack("!I", value[4:8])[0]
        self.assertEqual(header_size, len(value)-8)
        return json.loads(value[8:])

    def test_feed_reconstructs_sonar_once_and_publishes_every_transformed_point(self):
        self.feed.refresh()
        self.assertEqual(self.reconstructions, [("map", 7, 1203)])
        message = self.decoded()
        self.assertTrue(message["complete"])
        self.assertIsNone(message["data"]["next_cursor"])
        rows = message["data"]["entities"]
        self.assertEqual(len(rows), 1210)
        self.assertEqual(len({r["entity_id"] for r in rows}), 1210)
        sonar = [r for r in rows if r["data"].get("source") == "sonar"]
        self.assertEqual([r["entity_id"] for r in sonar],
                         ["map:ray:%05d" % i for i in range(1203)])
        np.testing.assert_allclose([r["data"]["xyz"] for r in sonar],
                                  [[10, 0, 1+i/1000] for i in range(1203)])
        self.assertTrue(all(r["revision"] == 7 for r in rows))
        # No changed atlas version means no second snapshot or reconstruction.
        self.feed.refresh()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(len(self.reconstructions), 1)

    def test_scoped_snapshot_is_identical_to_uncached_pagination(self):
        original = snapshot_message(self.query, limit=83)
        self.assertGreater(len(self.reconstructions), 1)
        self.reconstructions.clear()
        with self.atlas.snapshot_reads():
            scoped = snapshot_message(self.query, limit=83)
        self.assertEqual(scoped, original)
        self.assertEqual(self.reconstructions, [("map", 7, 1203)])

    def test_next_snapshot_reads_new_revision_and_new_ray(self):
        self.feed.refresh()
        self.add_rays(1203, 1, revision=8)
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("UPDATE submaps SET revision=8 WHERE id='map'")
            self.atlas.db.execute("UPDATE world SET revision=revision+1")
        self.feed.refresh()
        self.assertEqual(self.reconstructions, [("map", 7, 1203), ("map", 8, 1204)])
        rows = self.decoded()["data"]["entities"]
        self.assertEqual(len(rows), 1211)
        self.assertTrue(any(r["entity_id"] == "map:ray:01203" for r in rows))

    def test_failed_paged_read_discards_scope_before_retry(self):
        fail_once = True
        def query(operation, inputs):
            nonlocal fail_once
            if inputs.get("cursor") and fail_once:
                fail_once = False
                raise RuntimeError("injected page failure")
            return self.query(operation, inputs)
        self.service.query = query
        with self.assertRaisesRegex(RuntimeError, "injected page failure"):
            self.feed.refresh()
        self.assertEqual(self.messages, [])
        self.feed.refresh()
        self.assertEqual(self.reconstructions, [("map", 7, 1203), ("map", 7, 1203)])
        self.assertEqual(len(self.decoded()["data"]["entities"]), 1210)

    def test_concurrent_snapshot_has_its_own_scope(self):
        errors = []
        results = []
        with self.atlas.snapshot_reads():
            first = self.atlas.read(limit=500)
            def worker():
                try:
                    with self.atlas.snapshot_reads():
                        results.append(snapshot_message(self.query))
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            if errors:
                raise errors[0]
            page = self.atlas.read(cursor=first["next_cursor"], limit=500)
            self.assertEqual(len(page["entities"]), 500)
        self.assertEqual(len(results[0]["data"]["entities"]), 1210)
        self.assertEqual(self.reconstructions, [("map", 7, 1203), ("map", 7, 1203)])

    def test_cached_results_are_not_mutated_by_page_transform_or_consumer(self):
        with self.atlas.snapshot_reads():
            first = self.atlas.read(limit=500)
            first["entities"][-1]["data"]["xyz"][0] = -999
            repeated = self.atlas.read(limit=500)
            self.assertEqual(repeated["entities"][-1]["data"]["xyz"][0], 10)
            self.assertEqual(self.reconstructions, [("map", 7, 1203)])
        self.atlas.read(limit=500)
        self.assertEqual(len(self.reconstructions), 2)

    def test_nested_scope_restores_outer_snapshot_and_revision_keys_differ(self):
        with self.atlas.snapshot_reads():
            self.atlas.read(limit=500)
            with self.atlas.snapshot_reads():
                self.atlas.read(limit=500)
            self.atlas.read(limit=500)
            self.assertEqual(len(self.reconstructions), 2)
            with self.atlas.lock, self.atlas.db:
                self.atlas.db.execute("UPDATE submaps SET revision=8 WHERE id='map'")
                self.atlas.db.execute("UPDATE world SET revision=revision+1")
            self.atlas.read(limit=500)
        self.assertEqual(self.reconstructions[-1], ("map", 8, 1203))
        self.assertEqual(len(self.reconstructions), 3)


if __name__ == "__main__":
    unittest.main()
