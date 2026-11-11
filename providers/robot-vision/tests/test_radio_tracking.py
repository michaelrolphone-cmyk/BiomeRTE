"""Deterministic integration boundaries for asynchronous RF evidence.

These fixtures do not contact a camera, move the robot, or import the native
SLAM engine. Native relocalization geometry is tested separately.
"""
import copy
import contextlib
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radio_tracking import RadioTracking
from camera_telemetry import CameraHTTPError
from slam_backend import SlamBackend
from vision_config import DEFAULT_CONFIG
from vision_service import VisionService, CAPABILITIES
from test_vision_service import FakeCamera, FakeDetector, BackendFactory, calibration


def pose(x=0):
    transform = np.eye(4)
    transform[0, 3] = x
    return transform.tolist()


def keyframe(x=0, source="anchor-frame"):
    return {"T_map_camera": pose(x), "position": [x, 0, 0],
            "source_frame_id": source, "backend_frame_index": 1,
            "backend_keyframe_id": 0, "units": "arbitrary"}


def envelope(received, observed=None, *, rtt=0.0, boot="boot-one", fresh=True):
    observed = received if observed is None else observed
    rows = []
    for i in range(3):
        rows.append({"bssid": "02:00:00:00:00:%02x" % i,
                     "rssi_dbm": -40 - 10 * i,
                     "observed_uptime_ms": int(observed * 1000), "sample_count": 1,
                     "age_ms": (received - observed - rtt / 2) * 1000})
    return {
        "received_monotonic": received, "received_at": "2026-11-10T01:00:00+00:00",
        "request_duration_s": rtt, "fresh": fresh,
        "telemetry": {"receiver_id": "camera-one", "boot_id": boot,
                      "uptime_ms": int(received * 1000), "wifi": rows, "ble": []},
        "observations": [{"radio": "wifi", "index": i,
                          "source_observation_id": "event-%d" % i} for i in range(3)],
    }


class FakeSource:
    def __init__(self, value=None):
        self.value = value
        self.closed = False
        self.started = False
        self.light_calls = []
        self.network_calls = 0

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def snapshot(self):
        return copy.deepcopy(self.value)

    def status(self):
        return {"state": "fixture", "network_used": False}

    def light_status(self):
        self.network_calls += 1
        return {"on": False, "hardware_verified": False, "optical_readback": False}

    def set_light(self, update):
        self.network_calls += 1
        self.light_calls.append(copy.deepcopy(update))
        return {**update, "command_state_known": True,
                "hardware_verified": False, "optical_readback": False}


class RecordingModel:
    def __init__(self):
        self.training = []
        self.estimates = []
        self.reads = []
        self.closed = False
        self.result = {
            "state": "matched", "position": [0, 0, 0],
            "candidate_keyframe_ids": ["1"], "candidates": [], "clusters": [],
            "confidence": {"score": 0.7, "kind": "heuristic_not_probability"},
        }

    def train(self, *args, **kwargs):
        self.training.append(copy.deepcopy((args, kwargs)))
        return {"accepted": True, "reason": "fixture"}

    def estimate(self, *args, **kwargs):
        self.estimates.append(copy.deepcopy((args, kwargs)))
        return copy.deepcopy(self.result)

    def status(self):
        return {"sample_count": len(self.training), "state": "fixture"}

    def read_map(self, *args, **kwargs):
        self.reads.append(copy.deepcopy((args, kwargs)))
        return {"entries": [], "revision": 2, "geometry_revision": "fixture",
                "next_cursor": None}

    def close(self):
        self.closed = True


class RadioTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="biome-radio-tracking-")
        self.addCleanup(self.temp.cleanup)
        self.source = FakeSource()
        self.model = RecordingModel()
        self.radio = RadioTracking({"url": "fixture://no-network"},
                                   {"enabled": True}, self.temp.name,
                                   source=self.source, model=self.model)
        self.addCleanup(self.radio.close)
        self.radio.set_context("map-one", "cal-one", {"1": keyframe()}, 1)

    def record(self, tick, x=0, *, valid=True, epoch="stream-one",
               map_id="map-one", calibration_id="cal-one"):
        frame = {"frame_id": "frame-%s" % tick, "stream_epoch": epoch,
                 "calibration_id": calibration_id, "receive_decode_monotonic_s": tick}
        loc = {"valid": valid, "frame_id": frame["frame_id"], "map_id": map_id,
               "T_map_camera": pose(x), "reference_keyframe_id": "1"}
        self.radio.record_pose(frame, loc)

    def evaluate(self, received=100, observed=None, **kwargs):
        self.source.value = envelope(received, observed, **kwargs)
        self.radio.evaluate_once(now_s=received, now_utc_s=1000000)

    def test_old_scan_is_paired_with_historical_visual_pose_not_current_robot_position(self):
        self.record(100, 0)
        self.record(110, 10)
        self.evaluate(110, 100)
        args, kwargs = self.model.training[0]
        self.assertEqual(args[3]["timestamp_s"], 100)
        self.assertEqual(args[4]["T_keyframe_camera"][0][3], 0)
        self.assertTrue(args[3]["independently_validated"])
        self.assertEqual(args[3]["source"], "visual")

    def test_fresh_http_response_cannot_rejuvenate_old_rf_event(self):
        self.record(100)
        self.evaluate(120, 100)
        self.assertEqual(self.model.training, [])
        self.assertEqual(self.model.estimates, [])
        self.assertEqual(self.radio.prediction(now_s=120)["state"],
                         "insufficient_coherent_radio_sources")

    def test_invalid_or_other_context_pose_does_not_train(self):
        self.record(100, valid=False)
        self.record(100, map_id="other")
        self.record(100, calibration_id="other")
        self.evaluate()
        self.assertEqual(self.model.training, [])
        self.assertEqual(self.radio.status()["learning"]["accepted"], 0)

    def test_matching_radio_prediction_never_enters_visual_pose_history(self):
        self.evaluate()
        prediction = self.radio.prediction(now_s=100)
        self.assertEqual(prediction["position"], [0, 0, 0])
        self.assertFalse(prediction["valid_visual_pose"])
        self.assertFalse(prediction["heading_available"])
        self.assertFalse(prediction["navigation_usable"])
        self.assertEqual(self.model.training, [])
        self.assertEqual(self.radio.status()["pose_history_count"], 0)

    def test_network_round_trip_uncertainty_can_disqualify_pose_pair(self):
        self.record(100)
        self.evaluate(101.2, 100, rtt=1.2)
        self.assertEqual(self.model.training, [])
        self.assertEqual(self.radio.status()["learning"]["accepted"], 0)

    def test_separate_scan_times_are_not_merged_into_one_fingerprint(self):
        self.record(100)
        self.record(105)
        value = envelope(105)
        value["telemetry"]["wifi"][0]["age_ms"] = 5000
        self.source.value = value
        self.radio.evaluate_once(now_s=105, now_utc_s=1000000)
        self.assertEqual([len(item[0][2]["observations"]) for item in self.model.training], [1, 2])
        self.assertEqual(self.model.estimates, [])

    def test_duplicate_delivery_is_not_retrained_each_worker_tick(self):
        self.record(100)
        self.evaluate()
        self.radio.evaluate_once(now_s=100.2, now_utc_s=1000000.2)
        self.assertEqual(len(self.model.training), 1)
        self.assertEqual(len(self.model.estimates), 2)

    def test_still_fresh_scan_is_retried_when_visual_processing_finishes_later(self):
        self.evaluate()
        self.assertEqual(self.model.training, [])
        self.record(100)
        self.radio.evaluate_once(now_s=100.2, now_utc_s=1000000.2)
        self.assertEqual(len(self.model.training), 1)
        self.assertEqual(self.model.training[0][0][3]["timestamp_s"], 100)
        self.record(100.3)
        self.radio.evaluate_once(now_s=100.4, now_utc_s=1000000.4)
        self.assertEqual(len(self.model.training), 1)

    def test_receiver_calibration_identity_is_separate_from_model_settings(self):
        from radio_map import RadioMap
        source = FakeSource(envelope(100))
        root = Path(self.temp.name) / "receiver-calibration"
        radio = RadioTracking({"url": "fixture://no-network"},
                              {"enabled": True, "receiver_calibration_id": "antenna-v2",
                               "mapping": {"min_sources": 3}},
                              root, source=source)
        try:
            self.assertIsInstance(radio.model, RadioMap)
            radio.set_context("m", "c", {"1": keyframe()}, 1)
            with patch.object(radio.model, "estimate", return_value=self.model.result) as estimate:
                radio.evaluate_once(now_s=100, now_utc_s=1000000)
            self.assertEqual(estimate.call_args.args[2]["receiver_calibration_id"], "antenna-v2")
            self.assertEqual(radio.status()["receiver_calibration_id"], "antenna-v2")
        finally:
            radio.close()

    def test_map_or_calibration_switch_discards_previous_pose_history(self):
        self.record(100)
        self.radio.set_context("map-two", "cal-two", {"1": keyframe()}, 1)
        self.evaluate()
        self.assertEqual(self.model.training, [])
        self.assertEqual(self.model.estimates[0][0][:2], ("map-two", "cal-two"))

    def test_stream_epoch_change_discards_previous_capture_clock_history(self):
        self.record(100)
        self.record(110, epoch="stream-two", valid=False)
        self.evaluate(110, 100)
        self.assertEqual(self.model.training, [])

    def test_reused_keyframe_id_cannot_attach_old_observation_to_new_incarnation(self):
        self.record(100)
        self.radio.update_geometry("map-one", "cal-one", [
            {"op": "upsert", "kind": "keyframe", "entity_id": "1",
             "data": keyframe(0, source="replacement-frame")}], 2)
        self.evaluate()
        self.assertEqual(self.model.training, [])

    def test_loop_closure_invalidates_cached_inference_until_new_geometry_evaluation(self):
        self.record(100)
        self.evaluate()
        self.assertEqual(self.radio.prior(now_s=100), ["1"])
        self.radio.update_geometry("map-one", "cal-one", [
            {"op": "upsert", "kind": "keyframe", "entity_id": "1",
             "data": keyframe(8)}], 2)
        self.assertEqual(self.radio.prediction(now_s=100)["state"],
                         "geometry_changed_pending_refresh")
        self.assertEqual(self.radio.prior(now_s=100), [])
        self.radio.evaluate_once(now_s=100, now_utc_s=1000000)
        self.assertEqual(self.model.estimates[-1][0][3]["1"]["position"], [8, 0, 0])
        self.assertEqual(self.radio.prediction(now_s=100)["map_revision"], 2)

    def test_group_with_post_frame_observation_cannot_support_older_frame(self):
        value = envelope(100.8)
        value["telemetry"]["wifi"][0]["age_ms"] = 800
        self.source.value = value
        self.radio.evaluate_once(now_s=100.8, now_utc_s=1000000)
        self.assertEqual(self.radio.prior(now_s=100.8), ["1"])
        self.assertEqual(self.radio.prior(now_s=100.1), [])
        early = self.radio.prediction(now_s=100.1)
        self.assertEqual(early["state"], "radio_observations_after_frame")
        self.assertIsNone(early["position"])
        self.assertAlmostEqual(early["observation_span_s"], 0.8)

    def test_initial_missing_telemetry_is_explicit_in_current_context(self):
        self.radio.evaluate_once(now_s=100, now_utc_s=1000000)
        self.assertEqual(self.radio.prediction(now_s=100)["state"],
                         "telemetry_unavailable_or_stale")

    def test_candidate_prior_expires_sooner_than_readable_radio_evidence(self):
        self.record(100)
        self.evaluate()
        self.assertEqual(self.radio.prior(now_s=104), ["1"])
        self.assertEqual(self.radio.prior(now_s=106), [])
        self.assertEqual(self.radio.prediction(now_s=106)["state"], "matched")
        stale = self.radio.prediction(now_s=116)
        self.assertEqual(stale["state"], "radio_observations_stale")
        self.assertIsNone(stale["position"])

    def test_invalid_candidates_do_not_consume_live_prior_budget(self):
        anchors = {str(i): keyframe(i) for i in range(1, 20)}
        self.radio.set_context("map-one", "cal-one", anchors, 1)
        self.model.result["candidate_keyframe_ids"] = [
            "missing", "-1", "01", True, "999", *[str(i) for i in range(1, 20)], "1"]
        self.evaluate()
        self.assertEqual(self.radio.prior(now_s=100), [str(i) for i in range(1, 13)])

    def test_visual_comparison_is_separate_evidence_not_pose_mutation(self):
        self.evaluate()
        original = {"valid": True, "T_map_camera": pose(5)}
        value = self.radio.evidence(original, now_s=100)
        self.assertEqual(value["visual_comparison"]["state"], "disagreement")
        self.assertEqual(original, {"valid": True, "T_map_camera": pose(5)})
        self.assertFalse(value["navigation_usable"])
        self.assertIsNone(value["physical_capture_time_uncertainty_s"])

    def test_stale_telemetry_clears_prediction_without_training(self):
        self.record(100)
        self.evaluate()
        self.evaluate(101, fresh=False)
        self.assertEqual(len(self.model.training), 1)
        self.assertEqual(self.radio.prior(now_s=101), [])
        self.assertIsNone(self.radio.prediction(now_s=101)["position"])

    def test_incompatible_stored_database_does_not_abort_optional_model_startup(self):
        root = Path(self.temp.name) / "future-radio"
        folder = root / "radio"
        folder.mkdir(parents=True)
        path = folder / "radio.sqlite3"
        with sqlite3.connect(path) as database:
            database.execute("PRAGMA user_version=999")
        radio = RadioTracking({"url": "fixture://no-network"}, {"enabled": True},
                              root, source=FakeSource())
        try:
            radio.set_context("m", "c", {}, 0)
            self.assertIsNone(radio.model)
            self.assertEqual(radio.prediction(now_s=100)["state"], "model_unavailable")
            self.assertEqual(radio.status()["error"], "radio_model_unavailable:ValueError")
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 999)
        finally:
            radio.close()

    def test_invalid_model_settings_remain_explicit_before_storage_initialization(self):
        root = Path(self.temp.name) / "invalid-radio-settings"
        with self.assertRaises(ValueError):
            RadioTracking({"url": "fixture://no-network"},
                          {"enabled": True, "mapping": {"max_samples": 0}},
                          root, source=FakeSource())
        self.assertFalse((root / "radio").exists())

    def test_model_storage_failure_disables_only_optional_radio_learning(self):
        source = FakeSource()
        with patch("radio_map.RadioMap", side_effect=OSError("disk unavailable")):
            radio = RadioTracking({"url": "fixture://no-network"}, {"enabled": True},
                                  Path(self.temp.name) / "failed", source=source)
        try:
            radio.set_context("m", "c", {}, 0)
            radio.evaluate_once(now_s=100)
            self.assertEqual(radio.prediction(now_s=100)["state"], "model_unavailable")
            self.assertEqual(radio.status()["error"], "radio_model_unavailable:OSError")
            self.assertEqual(radio.read_map("m", "c", {})["availability"], "unavailable")
            self.assertFalse(source.light_status()["optical_readback"])
        finally:
            radio.close()

    def test_disabled_extension_does_not_create_database_or_train(self):
        root = Path(self.temp.name) / "disabled"
        radio = RadioTracking({"url": "fixture://no-network"}, {}, root,
                              source=FakeSource())
        try:
            radio.set_context("m", "c", {}, 0)
            radio.evaluate_once(now_s=100)
            self.assertEqual(radio.prediction(now_s=100)["state"], "disabled")
            self.assertFalse((root / "radio").exists())
        finally:
            radio.close()


class RadioServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="biome-radio-service-")
        self.addCleanup(self.temp.cleanup)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["camera"].update(url="fixture://no-network", source_id="fixture",
                                     expected_width=32, expected_height=24)
        self.config["calibration"] = calibration()
        self.config["radio"]["enabled"] = True
        self.source, self.model, self.factory = FakeSource(), RecordingModel(), BackendFactory()
        self.service = VisionService(self.config, self.temp.name, camera=FakeCamera(),
                                     detector=FakeDetector(), backend_factory=self.factory,
                                     radio_source=self.source, radio_model=self.model)
        self.addCleanup(self.service.close)

    def frame(self, tick=100, *, value=10, valid=True):
        frame_id = self.service.session_id + ":" + str(tick)
        metadata = {"frame_id": frame_id, "stream_epoch": "stream-one", "sequence": int(tick),
                    "width": 32, "height": 24, "capture_time": None,
                    "receive_decode_time": datetime.now(timezone.utc).isoformat()}
        self.service.backend.scripted.append({
            "localization": {"status": "tracking" if valid else "lost", "valid": valid,
                             "T_map_camera": pose(), "reference_keyframe_id": "1",
                             "units": "arbitrary"},
            "geometry": {"available": True, "complete_snapshot": False},
            "map_changes": [{"op": "upsert", "kind": "keyframe", "entity_id": "1",
                             "data": keyframe(source=frame_id)}],
            "observation_anchor": None,
        })
        return SimpleNamespace(image=np.full((24, 32, 3), value, np.uint8), metadata=metadata,
                               sequence=int(tick), decoded_monotonic=tick)

    def test_published_visual_pose_trains_worker_and_light_measurement_is_passive(self):
        captured = self.frame()
        original = captured.image.copy()
        snapshot = self.service.process_frame(captured)
        self.source.value = envelope(100)
        self.service.radio.evaluate_once(now_s=100, now_utc_s=1000000)
        self.assertEqual(len(self.model.training), 1)
        self.assertEqual(self.model.training[0][0][3]["source_frame_id"], snapshot["frame_id"])
        self.assertTrue(snapshot["frame"]["lighting"]["low_light_observed"])
        np.testing.assert_array_equal(captured.image, original)
        self.assertEqual(self.source.light_calls, [])
        self.assertEqual(self.source.network_calls, 0)
        self.assertFalse(snapshot["spatial_evidence"]["radio"]["navigation_usable"])

    def test_unpublished_pose_is_not_offered_for_training(self):
        captured = self.frame()
        with patch.object(self.service.store, "publish_observation", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.service.process_frame(captured)
        self.source.value = envelope(100)
        self.service.radio.evaluate_once(now_s=100, now_utc_s=1000000)
        self.assertEqual(self.model.training, [])

    def test_radio_failure_does_not_replace_valid_visual_pose(self):
        self.source.value = None
        self.service.radio.evaluate_once(now_s=100, now_utc_s=1000000)
        snapshot = self.service.process_frame(self.frame())
        self.assertTrue(snapshot["localization"]["valid"])
        self.assertEqual(snapshot["localization"]["T_map_camera"], pose())
        self.assertFalse(snapshot["spatial_evidence"]["radio"]["valid_visual_pose"])

    def test_map_read_is_pinned_to_requested_visual_revision(self):
        self.service.process_frame(self.frame())
        revision = self.service.revision
        response = self.service.query("robot.map.radio.read",
                                      {"revision": revision, "limit": 7, "source_id": "wifi:test"})
        args, kwargs = self.model.reads[-1]
        self.assertEqual(args[:2], (self.service.map_id, self.service.calibration["calibration_id"]))
        self.assertIn("1", args[2])
        self.assertEqual(kwargs["source_id"], "wifi:test")
        self.assertEqual(kwargs["limit"], 7)
        self.assertEqual(response["map_revision"], revision)
        self.assertEqual(response["units"], "arbitrary")

    def test_explicit_light_commands_preserve_camera_response_and_unknown_readback(self):
        state = self.service.query("robot.camera.light.status")
        self.assertFalse(state["camera_light"]["optical_readback"])
        update = {"on": True, "brightness": 32, "rgb": [255, 255, 255]}
        result = self.service.query("robot.camera.light.set", update)
        self.assertEqual(self.source.light_calls, [update])
        self.assertEqual(result["camera_light"]["brightness"], 32)
        self.assertFalse(result["camera_light"]["hardware_verified"])
        self.assertFalse(result["motion_commands"])

    def test_unknown_light_outcome_is_returned_without_automatic_retry(self):
        error = CameraHTTPError("camera_http_transport_error", outcome_known=False)
        with patch.object(self.source, "set_light", side_effect=error) as call:
            response = self.service.query("robot.camera.light.set", {"on": True})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(response["availability"], "unavailable")
        self.assertFalse(response["outcome_known"])
        self.assertFalse(response["command_state_known"])

    def test_mutating_provider_receipt_failures_preserve_unknown_effect(self):
        import biome_provider as provider
        for code in ("service_unavailable", "invalid_response", "response_too_large"):
            with self.subTest(code=code):
                output = io.StringIO()
                with patch.object(provider, "load_config", return_value={}), \
                     patch.object(provider, "Remote"), \
                     patch.object(provider, "execute", side_effect=provider.ServiceError(code, "fixture")), \
                     patch.object(sys, "stdin", io.StringIO('{"inputs":{"on":true}}')), \
                     contextlib.redirect_stdout(output):
                    result = provider.main(["execute", "--contract", "robot.camera.light.set:1"])
                self.assertEqual(result, 0)
                error = json.loads(output.getvalue())["result"]["error"]
                self.assertEqual(error["effect"], "unknown")
                self.assertIn("Inspect current state", error["next_action"])

    def test_light_driver_failure_preserves_camera_receipt(self):
        receipt = {"error": "light_driver_failed", "command_state_known": False}
        error = CameraHTTPError("camera_http_error", http_status=503,
                                response=receipt, outcome_known=False)
        with patch.object(self.source, "set_light", side_effect=error) as call:
            response = self.service.query("robot.camera.light.set", {"on": True})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(response["http_status"], 503)
        self.assertEqual(response["camera_response"], receipt)

    def test_checkpoint_reconciliation_invalidates_radio_anchors(self):
        self.service.process_frame(self.frame())
        self.source.value = envelope(100)
        self.service.radio.evaluate_once(now_s=100, now_utc_s=1000000)
        self.assertEqual(self.service.radio.prior(now_s=100), ["1"])
        self.service.backend.entities.pop(("keyframe", "1"))
        self.service.query("robot.map.checkpoint")
        self.assertEqual(self.service.radio.prior(now_s=100), [])
        self.assertNotIn("1", self.service.radio._anchors)

    def test_recovered_pending_map_changes_reach_radio_geometry_after_commit(self):
        change = {"op": "upsert", "kind": "keyframe", "entity_id": "22",
                  "data": keyframe(22)}
        with patch.object(self.service.store, "apply_map_update", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.service._commit_changes([change])
        self.assertNotIn("22", self.service.radio._anchors)
        self.service._commit_changes([])
        self.assertEqual(self.service.radio._anchors["22"]["position"], [22, 0, 0])

    def test_new_contracts_match_service_capabilities_and_light_is_mutating(self):
        from biome_provider import CONTRACTS, MUTATING_CONTRACTS
        self.assertEqual(len(CAPABILITIES), 29)
        for name in ("robot.radio.status", "robot.radio.localize", "robot.map.radio.read",
                     "robot.camera.light.status", "robot.camera.light.set"):
            self.assertIn(name, CAPABILITIES)
            self.assertIn(name + ":1", CONTRACTS)
        self.assertIn("robot.camera.light.set:1", MUTATING_CONTRACTS)
        self.assertEqual(CONTRACTS["robot.map.radio.read:1"]["inputs"]["cursor"], "str?")


class BackendRadioBoundaryTests(unittest.TestCase):
    def test_native_membership_is_checked_before_effective_six_candidate_budget(self):
        config = {"calibration": calibration()}
        with tempfile.TemporaryDirectory(prefix="biome-radio-backend-") as root:
            backend = SlamBackend(config, root)
            backend._map_id = "map-one"
            observed = []
            loop = SimpleNamespace(last_loop_kf_id=0, radio_relocalization_prior_version=1)
            runtime = SimpleNamespace(
                map=SimpleNamespace(update_lock=threading.RLock(),
                                    get_keyframes=lambda: [SimpleNamespace(id=i) for i in range(10, 18)]),
                loop_closing=loop, tracking=SimpleNamespace(state=SimpleNamespace(name="OK")),
                track=lambda *args: observed.append(loop.relocalization_prior_frame_ids))
            backend._slam = runtime
            backend.set_relocalization_prior(
                ["missing", "-1", "001", True, *range(10), *range(10, 18)], map_id="map-one")
            with patch.object(backend, "_read_entities", return_value={}), \
                 patch.object(backend, "_current_pose", return_value=(np.eye(4), "10")):
                backend.track(np.zeros((24, 32, 3), np.uint8), "frame-one", 1)
                backend.track(np.zeros((24, 32, 3), np.uint8), "frame-two", 2)
            self.assertEqual(observed, [(10, 11, 12, 13, 14, 15), ()])
            backend.set_relocalization_prior(["10"], map_id="different-map")
            self.assertEqual(backend._radio_prior, ())
            self.assertTrue(backend.status()["radio_prior"]["supported"])


if __name__ == "__main__":
    unittest.main()
