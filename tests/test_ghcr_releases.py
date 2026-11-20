import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SPEC = importlib.util.spec_from_file_location("ghcr_release",
    str(Path(__file__).resolve().parents[1] / "deploy/docker-services/ghcr.py"))
ghcr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ghcr)

class GHCRReleaseTests(unittest.TestCase):
    def run_data(self, **change):
        value = {"id": 10, "run_attempt": 1, "head_sha": "a" * 40,
                 "head_branch": "main", "event": "push", "status": "completed",
                 "conclusion": "success", "head_repository": {"full_name": "owner/runtime"},
                 "html_url": "https://github.com/owner/runtime/actions/runs/10"}
        value.update(change)
        return value

    def test_exact_successful_main_qualification(self):
        with patch.object(ghcr, "api", return_value={"workflow_runs": [self.run_data()]}):
            self.assertEqual(ghcr.qualified("owner/runtime", "ci.yml", "a"*40)["run_id"], 10)

    def test_other_commits_prs_forks_and_branches_do_not_qualify(self):
        for changes in [{"head_sha": "b"*40}, {"event": "pull_request"},
                        {"head_branch": "feature"}, {"head_repository": {"full_name": "fork/runtime"}}]:
            with self.subTest(changes=changes), patch.object(ghcr, "api",
                    return_value={"workflow_runs": [self.run_data(**changes)]}):
                with self.assertRaisesRegex(ValueError, "No qualification"):
                    ghcr.qualified("owner/runtime", "ci.yml", "a"*40)

    def test_newer_failure_or_running_attempt_blocks_old_success(self):
        for newer in [self.run_data(id=11, conclusion="failure"),
                      self.run_data(id=11, status="in_progress", conclusion=None)]:
            with patch.object(ghcr, "api", return_value={"workflow_runs": [newer, self.run_data()]}):
                with self.assertRaisesRegex(ValueError, "has not succeeded"):
                    ghcr.qualified("owner/runtime", "ci.yml", "a"*40)

    def test_private_or_absent_packages_are_preflighted(self):
        with patch.object(ghcr, "api", side_effect=[
                {"type": "User", "login": "owner"},
                {"name": "first", "package_type": "container", "visibility": "private", "id": 1},
                None]) as query:
            result = ghcr.private_packages("owner", ["second", "first"])
            self.assertEqual([x["state"] for x in result["packages"]], ["existing", "absent"])
            self.assertEqual(query.call_args_list[-1].kwargs, {"missing": True})

    def test_public_internal_unknown_visibility_rejected(self):
        for visibility in ["public", "internal", None]:
            with patch.object(ghcr, "api", side_effect=[
                    {"type": "User", "login": "owner"},
                    {"name": "image", "package_type": "container", "visibility": visibility}]):
                with self.assertRaisesRegex(ValueError, "private visibility"):
                    ghcr.private_packages("owner", ["image"])

    def test_postflight_does_not_allow_missing_package(self):
        with patch.object(ghcr, "api", side_effect=[
                {"type": "Organization", "login": "owner"},
                {"name": "image", "package_type": "container", "visibility": "private"}]) as query:
            ghcr.private_packages("owner", ["image"], require_existing=True)
            self.assertEqual(query.call_args_list[-1].args[0], "/orgs/owner/packages/container/image")
            self.assertEqual(query.call_args_list[-1].kwargs, {"missing": False})

    def test_auth_failure_never_discloses_response_or_token(self):
        secret = "test-secret-must-not-be-in-errors"
        error = HTTPError("https://api.github.com/user", 403, secret, {}, io.BytesIO(secret.encode()))
        with patch.dict(os.environ, {"GH_TOKEN": secret}), patch.object(ghcr, "urlopen", side_effect=error):
            with self.assertRaises(ValueError) as raised:
                ghcr.api("/user")
            self.assertNotIn(secret, str(raised.exception))
            self.assertIn("403", str(raised.exception))
