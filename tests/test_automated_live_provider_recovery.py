import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from biomerte.app import Application, initialize
from biomerte.store import uid
from tests import run_live_provider_recovery as runner
from tests.qualify_live_provider_recovery import CONSUMERS, FEEDS, assess


def streams(window=4):
    return {key: {"seconds": window, "feeds": {
        feed: {"records": 9, "max_gap_seconds": 0.5, "max_delivery_age_seconds": 0.2, "unclocked_records": 0,
               **({"decoded_frames": 9, "invalid_frames": 0} if feed.startswith("frame:")
                  else {"instrument_samples": {"imu.front" if feed.endswith("front") else "lidar.rear": 9},
                                "instrument_max_gap_seconds": {"imu.front" if feed.endswith("front") else "lidar.rear": 0.5}})}
        for feed in FEEDS}} for key in CONSUMERS}


class AutomatedQualification(unittest.IsolatedAsyncioTestCase):
    async def test_signed_failures_retain_only_bounded_diagnostics_for_each_call(self):
        site = runner.Site.__new__(runner.Site)
        def gateway(_url, rpc, **kwargs):
            args = rpc["params"]["arguments"]
            op = args["operation"]
            values = {"cap.submit": (429, "Managed controller is busy"),
                      "cap.lease": (404, "private lease details")}
            if op == "cap.status":
                result = {"structuredContent": {"id": "operation", "state": "succeeded", "lease": "lease",
                          "result": {"result": {"camera": {}}}}}
            else:
                status, detail = values[op]
                result = {"isError": True, "structuredContent": {"status": status, "retry_same_id": True,
                          "error_code": "CONTROLLER_RUNTIME_UNAVAILABLE", "private": "secret"},
                          "content": [{"type": "text", "text": detail}]}
            return {"id": rpc["id"], "result": result}
        site.gateway = SimpleNamespace(request=gateway, RESOURCE="https://example.invalid/",
                                       credentials=lambda: {"access_token": "secret"})
        proof = await runner.collect_action(site, "operation", 3)
        for item in [proof["submission"], proof["results"][0], proof["lease_check"]]:
            self.assertGreater(item["started_at"], 0)
            self.assertGreaterEqual(item["elapsed_seconds"], 0)
            self.assertTrue(item["request_id"])
        self.assertEqual(proof["submission"]["request_id"], "operation-submit")
        self.assertEqual(proof["submission"]["error_category"], "Managed controller is busy")
        self.assertEqual(proof["lease_check"]["error_category"], "signed_runtime")
        self.assertTrue(proof["lease_check"]["retry_same_id"])
        encoded = json.dumps(proof)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private", encoded)
        site.gateway.request = Mock(side_effect=type("HTTPFailure", (Exception,), {"status": 429})("private"))
        status, details = await site.call("cap.status", {"operation":"operation"}, "r")
        self.assertEqual((status, details), (429, {"error_category": "http_transport"}))

    async def test_real_combined_submission_releases_lease_and_preserves_a_lost_submit_response(self):
        with tempfile.TemporaryDirectory() as temp:
            initialize(temp)
            app = Application(temp)

            def call(name, args):
                return app.call(name, args, uid())

            call("cap.define", dict(name="robot.vision.metadata", inputs={}, outputs={"result": "obj"}))
            call("cap.offer", dict(contract="robot.vision.metadata:1", resource="vision", provider="camera", capacity=1))

            class Site:
                async def call(self, name, args, request_id):
                    value = call(name, args)
                    if name == "cap.submit":
                        for op, fields in [
                            ("cap.fenced", {"lease": value["lease"]}),
                            ("provider.claim", {"operation": value["id"]}),
                            ("provider.result", {"operation": value["id"], "state": "succeeded",
                                                 "result": {"result": {"camera": {"source_id": "actual-provider"}}}})]:
                            call(op, dict(fields, generation=value["generation"], provider="camera"))
                        return 503, {}
                    return 200, value

            proof = await runner.collect_action(Site(), "canary-one", 4)
            self.assertEqual(proof["lease_check"]["state"], "released")
            self.assertTrue(proof["results"][0]["has_result"])
            self.assertEqual(assess(dict(proof, streams=streams()), 4, 1, 1),
                             ["signed submission did not confirm an operation and lease (HTTP 503)"])

    async def test_single_operation_survives_outage_and_reconciliation_cannot_count_as_fresh_run(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            node = state / "node.json"
            node.write_text(json.dumps({"node": "alias", "stream_gateway_url": "wss://gateway.invalid"}))
            args = SimpleNamespace(state_dir=state, node_file=node, force=True, gateway_home=state,
                                   window_seconds=4, max_gap_seconds=1, max_delivery_age_seconds=1,
                                   bridge="ws://127.0.0.1:8093/api/live/vision")
            operations = []

            async def action(site, op, window):
                operations.append(op)
                if len(operations) == 1:
                    return dict(submission={"operation": op, "status": 503}, results=[], lease_check={})
                return dict(submission={"operation": op, "status": 200, "lease": "l"},
                            results=[{"status": 200, "operation": op, "lease": "l", "state": "succeeded", "has_result": True}],
                            lease_check={"status": 200, "operation": op, "lease": "l", "state": "released"})

            async def observe(public, bridge, node, window, ready):
                for event in ready.values():
                    event.set()
                return streams()

            with (patch("tests.qualify_signed_submission_concurrency.scheduled_pressure",
                        AsyncMock(return_value={"passed": True, "receipt": "fixture"})),
                  patch.object(runner, "release_binding", AsyncMock(return_value={"manifest_revision": "v1"})),
                  patch.object(runner, "Site"), patch.object(runner, "collect_action", action),
                  patch.object(runner, "observe_streams", observe), patch("builtins.print")):
                self.assertEqual(await runner.monitor(args), 1)
                self.assertEqual(await runner.monitor(args), 1)
                self.assertEqual(await runner.monitor(args), 0)
                self.assertEqual(operations[0], operations[1])
                self.assertNotEqual(operations[1], operations[2])
                args.force = False
                self.assertEqual(await runner.monitor(args), 0)
                self.assertEqual(len(operations), 3)
                self.assertTrue(json.loads((state / "latest.json").read_text())["passed"])
                self.assertFalse(json.loads((state / "previous.json").read_text())["passed"])

    async def test_placement_outage_preserves_the_last_observation_and_actual_http_status(self):
        class Unavailable(Exception):
            status = 503

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            node = state / "node.json"
            node.write_text(json.dumps({"node": "alias", "stream_gateway_url": "wss://gateway.invalid"}))
            args = SimpleNamespace(state_dir=state, node_file=node, force=False)
            previous = {"passed": False, "placement": {"manifest_revision": "rev1"},
                        "evidence": {"submission": {"status": 429}}, "finished": 10}
            runner.save(state / "latest.json", previous)
            with patch.object(runner, "release_binding", AsyncMock(side_effect=Unavailable())), patch("builtins.print"):
                self.assertEqual(await runner.monitor(args), 1)
            self.assertEqual(json.loads((state / "previous.json").read_text()), previous)
            self.assertEqual(json.loads((state / "last-measurement.json").read_text()), previous)
            current = json.loads((state / "latest.json").read_text())
            self.assertIn("HTTP 503", current["failures"][0])

    async def test_pressure_failure_keeps_streams_and_first_replies_across_forced_checks(self):
        from tests.test_signed_submission_concurrency_qualification import SiteFixture
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            node = state / "node.json"
            node.write_text(json.dumps({"node":"alias", "stream_gateway_url":"wss://gateway.invalid"}))
            args = SimpleNamespace(state_dir=state, node_file=node, force=True, gateway_home=state,
                                   window_seconds=4, max_gap_seconds=1, max_delivery_age_seconds=1,
                                   bridge="ws://127.0.0.1:8093/api/live/vision")
            site = SiteFixture()
            site.drop_first_reply = True
            site.call = AsyncMock(wraps=site.call)
            async def action(_, op, window):
                return dict(submission={"operation":op, "status":200, "lease":"metadata"},
                            results=[{"operation":op, "status":200, "lease":"metadata",
                                      "state":"succeeded", "has_result":True}],
                            lease_check={"operation":op, "lease":"metadata", "status":200, "state":"released"})
            async def observe(public, bridge, node, window, ready):
                for event in ready.values():
                    event.set()
                return streams()
            with (patch.object(runner, "Site", return_value=site),
                  patch.object(runner, "collect_action", action), patch.object(runner, "observe_streams", observe),
                  patch.object(runner, "release_binding", AsyncMock(return_value={"manifest_revision":"v1"})),
                  patch("builtins.print")):
                self.assertEqual(await runner.monitor(args), 1)
                before = json.loads((state / "latest.json").read_text())
                self.assertEqual(before["evidence"]["streams"], streams())
                self.assertEqual(before["evidence"]["lease_check"]["state"], "released")
                self.assertEqual(len(before["failures"]), 1)
                original = (state / before["signed_pressure"]["receipt"]).read_bytes()
                self.assertEqual(json.loads(original)["submissions"][0]["status"], 429)
                total = site.call.await_count
                self.assertEqual(await runner.monitor(args), 1)
                self.assertEqual(site.call.await_count, total)
                after = json.loads((state / "latest.json").read_text())
                self.assertEqual(after["signed_pressure"], before["signed_pressure"])
                self.assertEqual((state / after["signed_pressure"]["receipt"]).read_bytes(), original)
