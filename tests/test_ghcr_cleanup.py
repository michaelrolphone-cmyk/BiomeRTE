import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("cleanup", Path(__file__).resolve().parents[1] / "deploy/docker-services/ghcr_cleanup.py")
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)

class FakeAPI:
    def __init__(self, expected):
        self.state = copy.deepcopy(expected)
        self.calls = []
        self.add_unknown_after_delete = False
        self.replace_empty_package = False

    def package(self, name):
        if name not in self.state:
            return None
        state = self.state[name]
        value = state["metadata"]
        return {
            "id": value["id"] + (1 if self.replace_empty_package and not state["versions"] else 0),
            "name": value["name"], "package_type": value["package_type"], "visibility": value["visibility"],
            "owner": {"id": value["owner_id"], "login": value["owner_login"]},
            "repository": {"id": value["repository_id"], "full_name": value["repository"]},
            "created_at": value["created_at"], "version_count": len(state["versions"]),
        }

    @staticmethod
    def raw(value):
        return {"id": value["id"], "name": value["digest"], "created_at": value["created_at"],
                "updated_at": value["updated_at"], "metadata": {"container": {"tags": value["tags"]}}}

    def versions(self, name):
        return [self.raw(v) for v in self.state[name]["versions"]]

    def version(self, name, vid):
        return self.raw(next(v for v in self.state[name]["versions"] if v["id"] == vid))

    def delete_version(self, name, vid):
        self.calls.append(("version", name, vid))
        self.state[name]["versions"] = [v for v in self.state[name]["versions"] if v["id"] != vid]
        if self.add_unknown_after_delete:
            new = copy.deepcopy(self.state[name]["versions"][0])
            new["id"] = 999999999
            self.state[name]["versions"].append(new)
            self.add_unknown_after_delete = False
        return 204

    def delete_package(self, name):
        self.calls.append(("package", name))
        del self.state[name]
        return 204

    def anonymous(self, name, reference):
        self.calls.append(("anonymous", name, reference))
        return {"package": name, "reference": reference, "denied": True, "token_status": 403}

class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.expected = cleanup.load_expected()
        self.receipt = {"actions": [], "anonymous_checks": [], "complete": False}
        self.saved = []

    def save(self):
        self.saved.append(copy.deepcopy(self.receipt))

    def test_verify_only_and_global_preflight_never_mutate(self):
        api = FakeAPI(self.expected)
        cleanup.perform(api, self.expected, False, self.receipt, self.save)
        self.assertEqual(api.calls, [])
        api.state["portable-services"]["metadata"]["owner_id"] += 1
        with self.assertRaises(cleanup.Closed):
            cleanup.perform(api, self.expected, True, self.receipt, self.save)
        self.assertEqual(api.calls, [])

    def test_only_six_exact_versions_then_empty_packages_are_deleted(self):
        api = FakeAPI(self.expected)
        cleanup.perform(api, self.expected, True, self.receipt, self.save)
        versions = [call for call in api.calls if call[0] == "version"]
        packages = [call for call in api.calls if call[0] == "package"]
        self.assertEqual({call[2] for call in versions}, cleanup.VERSION_IDS)
        self.assertEqual(len(versions), 6)
        self.assertEqual({call[1] for call in packages}, set(cleanup.PACKAGE_IDS))
        self.assertEqual(len(packages), 3)
        self.assertEqual(len(self.receipt["anonymous_checks"]), 12)
        self.assertTrue(self.receipt["complete"])
        self.assertTrue(all(action["http_status"] == 204 for action in self.receipt["actions"]))
        self.assertTrue(any(saved["actions"] and "http_status" not in saved["actions"][-1]
                            for saved in self.saved))

    def test_concurrent_unknown_version_stops_before_another_delete(self):
        api = FakeAPI(self.expected)
        api.add_unknown_after_delete = True
        with self.assertRaises(cleanup.Closed):
            cleanup.perform(api, self.expected, True, self.receipt, self.save)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0][0], "version")
        self.assertFalse(self.receipt["complete"])

    def test_replaced_package_is_not_deleted(self):
        api = FakeAPI(self.expected)
        api.replace_empty_package = True
        with self.assertRaises(cleanup.Closed):
            cleanup.perform(api, self.expected, True, self.receipt, self.save)
        self.assertEqual(len(api.calls), 2)
        self.assertTrue(all(call[0] == "version" for call in api.calls))

    def test_delete_disabled_and_anonymous_check_excludes_workflow_token(self):
        api = cleanup.GitHub("synthetic-sensitive-token", self.expected, allow_delete=False)
        with patch.object(cleanup, "http") as request:
            with self.assertRaises(cleanup.Closed):
                api.delete_version("service-runner", 1601148152)
            request.assert_not_called()
        with patch.object(cleanup, "http", return_value=(403, None, "")) as request:
            result = api.anonymous("service-runner", "a81497cf0465-linux-arm64")
            self.assertTrue(result["denied"])
            self.assertNotIn("Authorization", request.call_args.kwargs["headers"])
            self.assertNotIn("synthetic-sensitive-token", str(request.call_args))

if __name__ == "__main__":
    unittest.main()
