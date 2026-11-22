import asyncio
import unittest

from tests.qualify_signed_submission_concurrency import qualify


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
