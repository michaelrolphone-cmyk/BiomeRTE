"""Bootstrap cadence exercises the production loader/observer and real SQLite.

Only time, navigation availability and the worker's event boundary are controlled.
The landmark SELECT and its row decoding run normally; no live service, camera,
network endpoint or production database is opened.
"""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_lidar_slam
from lidar_fusion import LidarFusion
from local_lidar_slam import LocalLidarSlam


class Clock:
    def __init__(self):
        self.now = 10.

    def monotonic(self):
        return self.now

    def wall(self):
        return 1700000000. + self.now


class MapStore:
    def __init__(self):
        self.maps = {}
        self.reads = []

    def read_map(self, map_id, *, kinds, cursor, limit):
        assert kinds == ["lidar_landmark"]
        self.reads.append(map_id)
        rows = self.maps.get(map_id, [])
        offset = int(cursor or 0)
        end = min(len(rows), offset + limit)
        return {"entities": copy.deepcopy(rows[offset:end]),
                "next_cursor": str(end) if end < len(rows) else None}


class Fixture:
    def __init__(self, count=1):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.clock = Clock()
        self.db = sqlite3.connect(self.path / "atlas.sqlite3")
        self.db.execute("CREATE TABLE survey_lidar_landmarks(id TEXT PRIMARY KEY,data TEXT)")
        self.queries = []
        self.rows_read = 0

        def trace(sql):
            if sql == "SELECT id,data FROM survey_lidar_landmarks":
                self.queries.append(self.clock.now)

        def row(cursor, values):
            if tuple(c[0] for c in cursor.description) == ("id", "data"):
                self.rows_read += 1
            return values

        self.db.set_trace_callback(trace)
        self.db.row_factory = row
        identity = np.eye(4).tolist()
        self.survey = {
            "world_id": "world-fixture", "survey_revision": 3,
            "reference": {"T_enu_body": identity, "T_enu_camera": identity},
        }
        self.write_survey()
        self.store = MapStore()
        self.store.maps["map-a"] = []
        self.commits = []
        self.service = SimpleNamespace(
            map_id="map-a", state_dir=self.path, store=self.store,
            lidar=SimpleNamespace(config={"revision": 7}),
            _pipeline_lock=threading.RLock(), _commit_changes=self.commit,
        )
        self.mode = "missing_navigation"
        self.seed = SimpleNamespace(
            atlas=SimpleNamespace(lock=threading.RLock(), db=self.db),
            current=self.current,
        )
        for i in range(count):
            rid, data = self.insert(i)
            self.store.maps["map-a"].append({
                "entity_id": rid, "data": dict(data, survey_seed=True,
                                              normal_enu=[0., 0., 0.]),
            })
        self.slam = LocalLidarSlam(self.service, self.seed)

    def write_survey(self):
        (self.path / "survey-world.json").write_text(json.dumps(self.survey))

    def insert(self, number):
        rid = "seed-" + str(number)
        data = {
            "world_id": "world-fixture", "survey_revision": 3, "rig_revision": 7,
            "xyz": [number * .01, 0., 1.], "zone": number % 16,
            "resolution": 16, "sensor_id": "front" if number % 2 == 0 else "rear",
        }
        self.db.execute("INSERT INTO survey_lidar_landmarks VALUES(?,?)",
                        (rid, json.dumps(data)))
        self.db.commit()
        return rid, data

    def commit(self, changes, origin):
        self.commits.append((copy.deepcopy(changes), origin))
        target = self.store.maps.setdefault(self.service.map_id, [])
        for change in changes:
            target[:] = [r for r in target if r["entity_id"] != change["entity_id"]]
            target.append({"entity_id": change["entity_id"],
                           "data": copy.deepcopy(change["data"])})

    def current(self, wall):
        if self.mode == "missing_navigation":
            return None
        pose = {
            "world_id": "world-fixture", "attitude_observed_at": wall,
            "rotation_enu_body": np.eye(3).tolist(), "position_enu_m": [0., 0., 0.],
        }
        if self.mode == "stale_attitude":
            pose["attitude_observed_at"] = wall - 5
        elif self.mode == "invalid_pose":
            pose["rotation_enu_body"] = [1., 0., 0.]
        return ({"world_body_pose": pose, "state": "moving"},
                copy.deepcopy(self.survey), {"id": "zero-fixture"}, wall)

    def packet(self, sequence):
        capture = 1000000 + sequence * 100000
        return {
            "receiver_id": "rear-fixture", "boot_id": "boot-fixture",
            "frame_id": "boot-fixture:" + str(capture),
            "capture_monotonic_us": capture,
        }

    def observe(self, sequence=0, points=False):
        packet = self.packet(sequence)
        rows = []
        if points:
            rows = [{
                "sensor_id": "front",
                "raw": {"observed_monotonic_us": packet["capture_monotonic_us"] - 1000,
                        "resolution": 16},
                "mount": {"T_front_camera_sensor_m": np.eye(4).tolist()},
                "points": [{"xyz_sensor_m": [i * .01, 0., 1.], "zone": i}
                           for i in range(8)],
            }]
        self.slam.observe(rows, packet, [self.clock.now, self.clock.now],
                          self.service.map_id)

    def close(self):
        self.db.close()
        self.directory.cleanup()


class BootstrapRefreshTests(unittest.TestCase):
    def fixture(self, count=1):
        fixture = Fixture(count)
        self.addCleanup(fixture.close)
        monotonic = mock.patch.object(local_lidar_slam.time, "monotonic",
                                      fixture.clock.monotonic)
        wall = mock.patch.object(local_lidar_slam.time, "time", fixture.clock.wall)
        monotonic.start()
        wall.start()
        self.addCleanup(monotonic.stop)
        self.addCleanup(wall.stop)
        return fixture

    def test_early_return_packets_do_not_rescan_landmarks(self):
        # 199 is the bounded retained atlas row count, not an assumed huge table.
        for mode in ("missing_navigation", "stale_attitude",
                     "invalid_pose", "insufficient_points"):
            with self.subTest(mode=mode):
                fixture = Fixture(199)
                try:
                    fixture.mode = mode
                    with mock.patch.object(local_lidar_slam.time, "monotonic",
                                           fixture.clock.monotonic), \
                         mock.patch.object(local_lidar_slam.time, "time",
                                           fixture.clock.wall):
                        for i in range(100):
                            fixture.clock.now = 10. + i / 10.
                            fixture.observe(i)
                    self.assertEqual(fixture.queries, [10., 12., 14., 16., 18.])
                    self.assertEqual(fixture.rows_read, 199 * 5)
                    self.assertEqual(len(fixture.slam.cells), 199)
                    self.assertEqual(fixture.slam.last, 0.,
                                     "failed scan preconditions must not advance the scan clock")
                    self.assertEqual(fixture.commits, [])
                finally:
                    fixture.close()

    def test_empty_reference_is_throttled_and_later_seed_is_visible(self):
        fixture = self.fixture(count=0)
        for i in range(20):
            fixture.clock.now = 10. + i / 10.
            fixture.observe(i)
        self.assertEqual(fixture.queries, [10.])
        self.assertIsNone(fixture.slam.reference)
        rid, _ = fixture.insert(8)
        fixture.clock.now = 11.99
        fixture.observe(20)
        self.assertEqual(fixture.queries, [10.])
        fixture.clock.now = 12.
        fixture.observe(21)
        self.assertEqual(fixture.queries, [10., 12.])
        self.assertEqual(set(fixture.slam.cells), {rid})
        self.assertEqual(fixture.slam.reference, fixture.survey)
        self.assertEqual([c[1] for c in fixture.commits], ["local_lidar_survey_seed"])

    def test_sqlite_failure_consumes_attempt_and_retries_after_deadline(self):
        fixture = self.fixture()
        denials = []

        def authorizer(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and table == "survey_lidar_landmarks":
                denials.append(fixture.clock.now)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        fixture.db.set_authorizer(authorizer)
        with self.assertRaises(sqlite3.DatabaseError):
            fixture.slam.bootstrap()
        for instant in (10.01, 10.5, 11.99):
            fixture.clock.now = instant
            fixture.slam.bootstrap()
        self.assertEqual(denials, [10.])
        fixture.clock.now = 12.
        with self.assertRaises(sqlite3.DatabaseError):
            fixture.slam.bootstrap()
        self.assertEqual(denials, [10., 12.])
        fixture.db.set_authorizer(None)
        fixture.slam.state["error"] = "previous SQLite failure"
        fixture.clock.now = 13.99
        fixture.slam.bootstrap()
        self.assertEqual(fixture.queries, [])
        fixture.clock.now = 14.
        fixture.slam.bootstrap()
        self.assertEqual(fixture.queries, [14.])
        self.assertEqual(fixture.slam.reference, fixture.survey)
        self.assertNotIn("error", fixture.slam.state)
        self.assertEqual(fixture.slam.last, 0.)

    def test_persisted_cells_without_reference_wait_for_successful_bootstrap(self):
        fixture = self.fixture()
        fixture.db.execute("DELETE FROM survey_lidar_landmarks")
        fixture.db.commit()
        fixture.mode = "valid"
        for i in range(20):
            fixture.clock.now = 10. + i / 10.
            fixture.observe(i, points=True)
        self.assertEqual(fixture.queries, [10.])
        self.assertTrue(fixture.slam.cells, "persisted map cells were loaded")
        self.assertIsNone(fixture.slam.reference)
        self.assertEqual(fixture.slam.last, 0.)
        fixture.insert(0)
        fixture.clock.now = 12.
        fixture.observe(21, points=True)
        self.assertEqual(fixture.queries, [10., 12.])
        self.assertEqual(fixture.slam.reference, fixture.survey)
        self.assertEqual(fixture.slam.last, 12.)

    def test_map_change_bypasses_only_current_map_deadline(self):
        fixture = self.fixture()
        fixture.store.maps["map-a"].append({
            "entity_id": "only-map-a",
            "data": {"survey_seed": False, "xyz": [1., 0., 1.],
                     "normal_enu": [0., 0., 1.]},
        })
        fixture.store.maps["map-b"] = fixture.store.maps["map-a"][:1]
        fixture.slam.last = 9.
        fixture.slam.last_pose_publish = 8.
        fixture.slam.bootstrap()
        fixture.slam.seen["front"] = "old"
        fixture.slam.candidates["old"].append("old")
        fixture.service.map_id = "map-b"
        fixture.clock.now = 10.1
        fixture.slam.bootstrap()
        self.assertEqual(fixture.slam.map_id, "map-b")
        self.assertNotIn("only-map-a", fixture.slam.cells)
        self.assertEqual(fixture.slam.seen, {})
        self.assertEqual(dict(fixture.slam.candidates), {})
        fixture.clock.now = 10.2
        fixture.slam.bootstrap()
        self.assertEqual(fixture.queries, [10., 10.1])
        fixture.service.map_id = "map-a"
        fixture.clock.now = 10.3
        fixture.slam.bootstrap()
        self.assertIn("only-map-a", fixture.slam.cells)
        self.assertEqual(fixture.store.reads, ["map-a", "map-b", "map-a"])
        self.assertEqual(fixture.queries, [10., 10.1, 10.3])
        self.assertEqual((fixture.slam.last, fixture.slam.last_pose_publish), (9., 8.))

    def test_scan_attempt_clock_does_not_control_reference_poll(self):
        fixture = self.fixture()
        fixture.mode = "valid"
        fixture.observe(0, points=True)
        self.assertEqual(fixture.slam.last, 10.)
        self.assertEqual(fixture.slam.state["status"], "no_observable_scan_match")
        fixture.clock.now = 11.9
        fixture.observe(1, points=True)
        self.assertEqual(fixture.slam.last, 11.9)
        fixture.clock.now = 12.
        fixture.observe(2, points=True)
        self.assertEqual(fixture.queries, [10., 12.])
        self.assertEqual(fixture.slam.last, 12.)
        fixture.clock.now = 12.1
        fixture.observe(2, points=True)  # duplicate head identity: no new scan attempt
        self.assertEqual(fixture.slam.last, 12.)
        self.assertEqual(fixture.slam.last_pose_publish, 0.)
        self.assertEqual(fixture.commits, [])

    def run_worker(self, fixture, *, packets):
        # Exercise the production _run scheduler; only its wake/stop boundary is fake.
        fusion = object.__new__(LidarFusion)
        fusion.lock = threading.RLock()
        fusion.stop = threading.Event()
        fusion.pending = None
        fusion.survey_landmarks = fixture.seed
        fusion.local_slam = fixture.slam
        fusion.error = None
        ticks = iter(range(21))

        def wait(timeout):
            i = next(ticks)
            fixture.clock.now = 10. + i / 5.
            fusion.pending = (i,) if packets else None
            if i == 20:
                fusion.stop.set()

        fusion.event = SimpleNamespace(wait=wait, clear=lambda: None)
        fusion.process = lambda i: fixture.observe(i)
        fusion._run()

    def test_worker_and_packet_paths_share_the_same_attempt_clock(self):
        fixture = self.fixture(count=199)
        self.run_worker(fixture, packets=True)
        self.assertEqual(fixture.queries, [10., 12., 14.])
        self.assertEqual(fixture.rows_read, 199 * 3)
        self.assertEqual(fixture.slam.last, 0.)
        self.assertNotIn("error", fixture.slam.state)

    def test_worker_refreshes_without_packets(self):
        fixture = self.fixture(count=0)
        self.run_worker(fixture, packets=False)
        self.assertEqual(fixture.queries, [10., 12., 14.])
        self.assertIsNone(fixture.slam.reference)
        self.assertEqual(fixture.slam.last, 0.)


if __name__ == "__main__":
    unittest.main()
