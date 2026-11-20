import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("cleanup", Path(__file__).resolve().parents[1] / "deploy/docker-services/ghcr_cleanup.py")
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)

class FakeAPI:
    def __init__(self, expected):
        self.state = copy.deepcopy(expected)
        self.calls = []
        self.inventory_reads = {}
        self.package_reads = {}
        self.inject_unknown_on_recheck = False
        self.replace_on_recheck = False
        self.delete_status = 204

    def package(self, name):
        if name not in self.state:
            return None
        self.package_reads[name] = self.package_reads.get(name, 0) + 1
        state = self.state[name]
        value = state["metadata"]
        replaced = self.replace_on_recheck and self.package_reads[name] == 2
        return {
            "id": value["id"] + (1 if replaced else 0), "name": value["name"],
            "package_type": value["package_type"], "visibility": value["visibility"],
            "owner": {"id": value["owner_id"], "login": value["owner_login"]},
            "repository": {"id": value["repository_id"], "full_name": value["repository"]},
            "created_at": value["created_at"], "version_count": len(state["versions"]),
        }

    @staticmethod
    def raw(value):
        return {"id": value["id"], "name": value["digest"], "created_at": value["created_at"],
                "updated_at": value["updated_at"], "metadata": {"container": {"tags": value["tags"]}}}

    def versions(self, name):
        self.inventory_reads[name] = self.inventory_reads.get(name, 0) + 1
        if self.inject_unknown_on_recheck and self.inventory_reads[name] == 2:
            value = copy.deepcopy(self.state[name]["versions"][0])
            value["id"] = 999999999
            self.state[name]["versions"].append(value)
            self.inject_unknown_on_recheck = False
        return [self.raw(v) for v in self.state[name]["versions"]]

    def delete_package(self, name):
        self.calls.append(("package", name))
        if self.delete_status == 204:
            del self.state[name]
        return self.delete_status

    def anonymous(self, name, reference):
        self.calls.append(("anonymous", name, reference))
        return {"package": name, "reference": reference, "denied": True, "token_status": 403}

class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.original = cleanup.load_expected()
        self.remaining = cleanup.load_resume_expected(self.original)
        self.receipt = {"actions": [], "anonymous_checks": [], "complete": False}
        self.saved = []

    def save(self):
        self.saved.append(copy.deepcopy(self.receipt))

    def run_cleanup(self, api, execute=True):
        cleanup.perform(api, self.remaining, self.original, execute, self.receipt, self.save)

    def test_exact_prior_history_and_hash_are_required(self):
        prior = json.loads(cleanup.PRIOR_RECEIPT.read_bytes())
        cleanup.validate_resume_proof(prior, self.original)
        for mutation in ("first_status", "second_status", "first_id", "workflow", "extra_action"):
            value = copy.deepcopy(prior)
            if mutation == "first_status":
                value["actions"][0]["http_status"] = 400
            elif mutation == "second_status":
                value["actions"][1]["http_status"] = 204
            elif mutation == "first_id":
                value["actions"][0]["version_id"] = 1601148152
            elif mutation == "workflow":
                value["workflow_sha"] = "0" * 40
            else:
                value["actions"].append(copy.deepcopy(value["actions"][0]))
            with self.subTest(mutation=mutation), self.assertRaises(cleanup.Closed):
                cleanup.validate_resume_proof(value, self.original)
        with tempfile.TemporaryDirectory() as folder:
            changed = Path(folder) / "prior.json"
            changed.write_bytes(cleanup.PRIOR_RECEIPT.read_bytes() + b" ")
            with self.assertRaisesRegex(cleanup.Closed, "hash mismatch"):
                cleanup.load_resume_expected(self.original, changed)

    def test_verify_only_and_exact_remaining_inventory_never_mutate(self):
        api = FakeAPI(self.remaining)
        self.run_cleanup(api, False)
        self.assertEqual(api.calls, [])
        for state in (self.original, self.remaining):
            api = FakeAPI(state)
            if state is self.remaining:
                api.state["portable-services"]["versions"].pop()
            with self.assertRaises(cleanup.Closed):
                self.run_cleanup(api)
            self.assertEqual(api.calls, [])

    def test_only_three_exact_packages_deleted_and_all_original_references_checked(self):
        api = FakeAPI(self.remaining)
        self.run_cleanup(api)
        packages = [call for call in api.calls if call[0] == "package"]
        self.assertEqual(len(packages), 3)
        self.assertEqual({call[1] for call in packages}, set(cleanup.PACKAGE_IDS))
        self.assertEqual({vid for action in self.receipt["actions"] for vid in action["remaining_version_ids"]},
                         cleanup.VERSION_IDS - {cleanup.REMOVED_VERSION_ID})
        self.assertTrue(all(action["http_status"] == 204 and action["package_absent_after"]
                            for action in self.receipt["actions"]))
        self.assertEqual(len(self.receipt["anonymous_checks"]), 12)
        self.assertTrue(self.receipt["complete"])
        self.assertTrue(any(saved["actions"] and "http_status" not in saved["actions"][-1]
                            for saved in self.saved))
        self.assertEqual(sum(len(p["versions"]) for p in self.original.values()), 6)

    def test_unknown_version_during_immediate_recheck_prevents_any_delete(self):
        api = FakeAPI(self.remaining)
        api.inject_unknown_on_recheck = True
        with self.assertRaises(cleanup.Closed):
            self.run_cleanup(api)
        self.assertEqual(api.calls, [])

    def test_replacement_package_id_prevents_any_delete(self):
        api = FakeAPI(self.remaining)
        api.replace_on_recheck = True
        with self.assertRaises(cleanup.Closed):
            self.run_cleanup(api)
        self.assertEqual(api.calls, [])

    def test_package_error_stops_without_retry_or_fallback(self):
        api = FakeAPI(self.remaining)
        api.delete_status = 400
        with self.assertRaisesRegex(cleanup.Closed, "HTTP 204"):
            self.run_cleanup(api)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(self.receipt["actions"][0]["http_status"], 400)
        self.assertFalse(self.receipt["complete"])

    def test_version_deletes_disabled_and_anonymous_check_excludes_workflow_token(self):
        api = cleanup.GitHub("synthetic-sensitive-token", self.remaining, allow_delete=True)
        with patch.object(cleanup, "http") as request:
            with self.assertRaises(cleanup.Closed):
                api.request(api.path("service-runner") + "/versions/1601148152", "DELETE")
            request.assert_not_called()
        api = cleanup.GitHub("synthetic-sensitive-token", self.remaining, allow_delete=False)
        with patch.object(cleanup, "http") as request:
            with self.assertRaises(cleanup.Closed):
                api.delete_package("service-runner")
            request.assert_not_called()
        with patch.object(cleanup, "http", return_value=(403, None, "")) as request:
            self.assertTrue(api.anonymous("service-runner", "a81497cf0465-linux-arm64")["denied"])
            self.assertNotIn("Authorization", request.call_args.kwargs["headers"])
            self.assertNotIn("synthetic-sensitive-token", str(request.call_args))

if __name__ == "__main__":
    unittest.main()
