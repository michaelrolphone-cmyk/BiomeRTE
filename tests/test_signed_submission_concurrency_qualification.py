import asyncio
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from tests.qualify_signed_submission_concurrency import (
    qualify, qualify_release, import_verified_pressure, pressure_directory,
    reconcile_pressure, scheduled_pressure,
)


class SiteFixture:
    def __init__(self):
        self.pending = set()
        self.release = asyncio.Event()
        self.progress_during_burst = False
        self.drop_first_reply = False

    async def call(self, name, args, request_id):
        if name == "cap.submit":
            op = args["operation"]
            if not op.endswith("-baseline"):
                self.pending.add(op)
                if len(self.pending) == 16:
                    self.release.set()
                await self.release.wait()
                if self.drop_first_reply and op.endswith("-00"):
                    return 429, {"error_category": "Managed controller is busy"}
            return 200, {"id": op, "state": "succeeded", "lease": "lease-" + op}
        if name == "cap.status":
            op = args["operation"]
            if op.endswith("-baseline"):
                self.progress_during_burst = bool(self.pending)
            return 200, {"id": op, "state": "succeeded", "lease": "lease-" + op,
                         "result": {"value": op}}
        lease = args["lease"]
        return 200, {"id": lease, "task": lease.removeprefix("lease-"), "state": "released"}


class SignedConcurrencyQualification(unittest.IsolatedAsyncioTestCase):
    async def test_independent_jobs_and_existing_progress_are_all_checked(self):
        site = SiteFixture()
        proof = await asyncio.wait_for(qualify(site, "run", stagger=0), 5)
        self.assertTrue(proof["passed"], proof)
        self.assertEqual(len(site.pending), 16)
        self.assertTrue(site.progress_during_burst)
        self.assertEqual(len(proof["readback"]), 16)

    async def test_reconciliation_cannot_hide_an_initial_rejection(self):
        site = SiteFixture()
        site.drop_first_reply = True
        proof = await asyncio.wait_for(qualify(site, "run", "prior", "lease-prior", stagger=0), 5)
        self.assertFalse(proof["passed"])
        self.assertEqual(proof["submissions"][0]["status"], 429)
        self.assertEqual(proof["submissions"][0]["error_category"], "Managed controller is busy")
        self.assertTrue(proof["readback"][0]["action"]["echo"])
        self.assertEqual(proof["readback"][0]["release"]["state"], "released")

    async def test_placement_change_fails_a_successful_wave_and_preserves_its_evidence(self):
        with patch("tests.qualify_signed_submission_concurrency.release_binding", new=AsyncMock(
                side_effect=[{"grant_digest": "original"}, {"grant_digest": "successor"}])):
            proof = await qualify_release(SiteFixture(), {"node": "fixture",
                "stream_gateway_url": "wss://example.invalid"}, "run", None, None)
        self.assertFalse(proof["passed"])
        self.assertEqual(proof["placement"]["grant_digest"], "original")
        self.assertEqual(proof["final_placement"]["grant_digest"], "successor")
        self.assertEqual(len(proof["readback"]), 16)


class ScheduledReleasePressure(unittest.IsolatedAsyncioTestCase):
    async def test_once_per_exact_placement_with_first_receipts_and_no_same_placement_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            site = SiteFixture()
            site.call = AsyncMock(wraps=site.call)
            before = {"manifest_revision":"v1", "grant_digest":"a"}
            after = dict(before, grant_digest="b")
            current = AsyncMock(side_effect=[before, after])
            result = await scheduled_pressure(state, before, site, current)
            self.assertTrue(result["passed"], result)
            first = json.loads((state / result["receipt"]).read_text())
            self.assertEqual(len(first["submissions"]), 16)
            self.assertEqual(len(first["readback"]), 16)
            self.assertEqual(sum(c.args[0] == "cap.submit" for c in site.call.await_args_list), 17)
            count = site.call.await_count
            self.assertEqual(await scheduled_pressure(state, before, site, current), result)
            self.assertEqual(site.call.await_count, count)
            fresh = await scheduled_pressure(state, after, site, current)
            self.assertTrue(fresh["passed"])
            self.assertNotEqual(fresh["run_id"], result["run_id"])
            self.assertEqual(sum(c.args[0] == "cap.submit" for c in site.call.await_args_list), 34)
            self.assertEqual(json.loads((state / result["receipt"]).read_text()), first)

    async def test_old_refusal_stays_failed_after_correct_readback_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            placement = {"grant_digest":"a"}
            site = SiteFixture()
            site.drop_first_reply = True
            result = await scheduled_pressure(state, placement, site, AsyncMock(return_value=placement))
            self.assertFalse(result["passed"])
            path = state / result["receipt"]
            original = path.read_bytes()
            self.assertEqual(json.loads(original)["submissions"][0]["status"], 429)
            self.assertTrue(json.loads(original)["readback"][0]["action"]["echo"])
            site.call = AsyncMock(side_effect=AssertionError("must not replay confirmed operations"))
            self.assertEqual(await scheduled_pressure(state, placement, site, AsyncMock()), result)
            await reconcile_pressure(state, placement, site)
            self.assertEqual(path.read_bytes(), original)
            site.call.assert_not_awaited()

    async def test_interrupted_attempt_recovers_only_started_ids_without_submitting(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            placement = {"grant_digest":"a"}
            folder = pressure_directory(state, placement)
            folder.mkdir(parents=True)
            (folder / "started.json").write_text(json.dumps(dict(run_id="original", placement=placement,
                receipts={"original-09/started":True, "original-09/submission":{"status":503}})))
            site = SiteFixture()
            site.call = AsyncMock(wraps=site.call)
            result = await scheduled_pressure(state, placement, site, AsyncMock())
            self.assertFalse(result["passed"])
            original = (state / result["receipt"]).read_bytes()
            await reconcile_pressure(state, placement, site)
            followup = json.loads((folder / "reconciled.json").read_text())
            self.assertEqual([r["operation"] for r in followup["readback"]], ["original-09"])
            self.assertEqual(followup["readback"][0]["release"]["state"], "released")
            await reconcile_pressure(state, placement, site)
            self.assertEqual([c.args[0] for c in site.call.await_args_list], ["cap.status", "cap.lease"])
            self.assertEqual((state / result["receipt"]).read_bytes(), original)
            self.assertEqual(json.loads(original)["partial_receipts"]["original-09/submission"]["status"],503)

    async def test_existing_wave_import_requires_every_receipt_and_exact_unchanged_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            placement = {"grant_digest":"a"}
            proof = await qualify(SiteFixture(), "observed", stagger=0)
            proof.update(placement=placement, final_placement=placement)
            with self.assertRaises(ValueError):
                import_verified_pressure(state, {"grant_digest":"other"}, proof)
            first = proof["submissions"][2]
            saved = first["lease"]
            first["lease"] = "other-job-lease"
            with self.assertRaises(ValueError):
                import_verified_pressure(state, placement, proof)
            first["lease"] = saved
            import_verified_pressure(state, placement, proof)
            import_verified_pressure(state, placement, proof)
            replacement = await qualify(SiteFixture(), "other-observed", stagger=0)
            replacement.update(placement=placement, final_placement=placement)
            with self.assertRaisesRegex(ValueError, "already exists"):
                import_verified_pressure(state, placement, replacement)
            site = AsyncMock()
            result = await scheduled_pressure(state, placement, site, AsyncMock())
            self.assertEqual(result["run_id"], "observed")
            self.assertTrue(result["passed"])
            site.call.assert_not_awaited()
