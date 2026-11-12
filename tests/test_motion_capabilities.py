import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from biomerte.motion_model import MotionModel

path=Path(__file__).resolve().parents[1]/"providers/rover-motion/biome_provider.py"
spec=importlib.util.spec_from_file_location("motion_capability_provider",path)
provider=importlib.util.module_from_spec(spec);spec.loader.exec_module(provider)

class NativeMotionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.database=Path(self.directory.name)/"model.sqlite"
        self.model=MotionModel()
        self.db=sqlite3.connect(self.database)
        self.db.executescript("CREATE TABLE model(id INTEGER PRIMARY KEY,payload TEXT,updated_utc REAL); CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT,payload TEXT,received_monotonic REAL);")
        self.save()
        self.backend=provider.ModelBackend(self.database)

    def tearDown(self):
        self.db.close();self.directory.cleanup()

    def save(self):
        self.db.execute("INSERT OR REPLACE INTO model VALUES(1,?,?)",(json.dumps(self.model.export()),time.time()))
        self.db.execute("INSERT INTO events(kind,payload,received_monotonic) VALUES('vision',?,?)",
            (json.dumps({"frame":{"capture_host_monotonic_interval_s":[time.monotonic()-.1,time.monotonic()]}}),time.monotonic()))
        self.db.commit()

    def test_generic_contracts_have_no_host_placement_requirement(self):
        self.assertEqual(set(provider.CONTRACTS),{"robot.motion.model:1","robot.motion.predict:1"})
        self.assertEqual(provider.CONTRACTS["robot.motion.predict:1"]["inputs"]["pulses"],"list")
        for contract in provider.CONTRACTS.values():
            self.assertNotIn("provider",contract["inputs"])
            self.assertNotIn("host",contract["inputs"])

    def test_model_export_is_the_portable_persisted_snapshot(self):
        self.model.advance(self.model.state,5,.8);self.save()
        result=self.backend.query("robot.motion.model:1",{"include_parameters":True})
        self.assertEqual(result["parameters"],self.model.export())
        self.assertEqual(result["state"]["steering_latch"],self.model.state["steering_latch"])
        self.assertFalse(result["executes_motion"])
        self.assertTrue(result["freshness"]["live_state_available"])

    def test_native_prediction_keeps_steering_and_never_writes_model(self):
        before=self.db.execute("SELECT payload FROM model").fetchone()[0]
        result=self.backend.query("robot.motion.predict:1",{"pulses":[{"mask":5,"duration_s":.8},{"mask":0,"duration_s":.3},{"mask":1,"duration_s":.7}]})
        latches=[s["state"]["steering_latch"] for s in result["steps"]]
        self.assertGreater(latches[0],.5)
        self.assertEqual(latches[0],latches[1]);self.assertEqual(latches[1],latches[2])
        self.assertFalse(result["executes_motion"])
        self.assertIsNone(result["steps"][-1]["distance_m"])
        self.assertEqual(before,self.db.execute("SELECT payload FROM model").fetchone()[0])

    def test_revision_guard_and_inputs_are_explicit(self):
        with self.assertRaises(provider.QueryError) as error:
            self.backend.query("robot.motion.predict:1",{"model_revision":99,"pulses":[{"mask":1,"duration_s":.2}]})
        self.assertEqual(error.exception.code,"model_revision_conflict")
        for inputs in [{"extra":1},{"include_parameters":"yes"}]:
            with self.assertRaises(provider.QueryError):self.backend.query("robot.motion.model:1",inputs)
        with self.assertRaises(provider.QueryError):
            self.backend.query("robot.motion.predict:1",{"pulses":[{"mask":3,"duration_s":1}]})

    def test_stale_capture_marks_live_motion_unknown(self):
        self.model.state.update(steering_known=True,speed_m_s=.3,yaw_rate_deg_s=4.)
        self.save()
        self.db.execute("UPDATE events SET payload=?", (json.dumps({"frame":{"capture_host_monotonic_interval_s":[time.monotonic()-10,time.monotonic()-9]}}),))
        self.db.commit()
        result=self.backend.query("robot.motion.model:1",{})
        self.assertFalse(result["freshness"]["live_state_available"])
        self.assertFalse(result["state"]["steering_known"])
        self.assertIsNone(result["state"]["speed_m_s"])

    def test_missing_model_is_a_query_failure(self):
        with self.assertRaises(provider.QueryError) as error:
            provider.ModelBackend(Path(self.directory.name)/"missing.sqlite").query("robot.motion.model:1",{})
        self.assertEqual(error.exception.code,"model_unavailable")

class RunnerRegistrationTests(unittest.TestCase):
    def test_motion_handlers_use_existing_durable_runner(self):
        from biomerte import supervised_commands
        previous=supervised_commands.CONTRACTS
        try:
            provider.register_runner_contracts()
            self.assertTrue(supervised_commands.supported({"contract":"robot.motion.model:1"}))
            self.assertTrue(supervised_commands.supported({"contract":"robot.motion.predict:1"}))
            for name in previous:self.assertIn(name,supervised_commands.CONTRACTS)
            once=supervised_commands.CONTRACTS
            provider.register_runner_contracts()
            self.assertEqual(once,supervised_commands.CONTRACTS)
        finally:supervised_commands.CONTRACTS=previous
