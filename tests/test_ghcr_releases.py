import importlib.util
import io
import json
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
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

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
                None,
                {"full_name": "owner/publisher", "private": True, "visibility": "private"}]) as query:
            result = ghcr.private_packages("owner", ["second", "first"], repository="owner/publisher")
            self.assertEqual([x["state"] for x in result["packages"]], ["existing", "absent"])
            self.assertEqual(query.call_args_list[2].kwargs, {"missing": True})
            self.assertEqual(result["packages"][1]["creation_repository"]["name"], "owner/publisher")

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

    def test_absent_package_from_public_internal_or_unknown_repository_fails_closed(self):
        for visibility, private in [("public", False), ("internal", True), (None, None)]:
            with self.subTest(visibility=visibility), patch.object(ghcr, "api", side_effect=[
                    {"type": "User", "login": "owner"}, None,
                    {"full_name": "owner/publisher", "visibility": visibility, "private": private}]):
                with self.assertRaisesRegex(ValueError, "Refusing private package creation") as raised:
                    ghcr.private_packages("owner", ["image"], repository="owner/publisher")
                self.assertIn("visibility="+json.dumps(visibility), str(raised.exception))

    def test_absent_package_without_publishing_context_fails_closed(self):
        with patch.object(ghcr, "api", side_effect=[{"type": "User", "login": "owner"}, None]) as query:
            with self.assertRaisesRegex(ValueError, "publishing repository is required"):
                ghcr.private_packages("owner", ["image"])
            self.assertEqual(query.call_count, 2)

    def test_existing_private_package_does_not_require_private_publisher(self):
        with patch.object(ghcr, "api", side_effect=[
                {"type": "User", "login": "owner"},
                {"name": "image", "package_type": "container", "visibility": "private"}]) as query:
            result = ghcr.private_packages("owner", ["image"], repository="owner/public-source")
            self.assertEqual(result["packages"][0]["visibility"], "private")
            self.assertEqual(query.call_count, 2)

    def test_workflow_repository_cannot_be_overridden_for_creation(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/public-source"}), \
                patch.object(ghcr, "api", side_effect=[{"type": "User", "login": "owner"}, None]) as query:
            with self.assertRaisesRegex(ValueError, "does not match"):
                ghcr.private_packages("owner", ["image"], repository="owner/private-publisher")
            self.assertEqual(query.call_count, 2)

    def test_workflow_repository_is_verified_for_creation(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/private-publisher"}), \
                patch.object(ghcr, "api", side_effect=[
                    {"type": "User", "login": "owner"}, None,
                    {"full_name": "owner/private-publisher", "visibility": "private", "private": True}]) as query:
            result = ghcr.private_packages("owner", ["image"])
            self.assertEqual(result["packages"][0]["creation_repository"]["name"], "owner/private-publisher")
            self.assertEqual(query.call_args.args[0], "/repos/owner/private-publisher")

    def test_missing_postflight_never_becomes_permission_to_create(self):
        with patch.object(ghcr, "api", side_effect=[{"type": "User", "login": "owner"}, None]):
            with self.assertRaisesRegex(ValueError, "Required private package"):
                ghcr.private_packages("owner", ["image"], require_existing=True)

    def test_rejected_package_error_records_actual_visibility(self):
        with patch.object(ghcr, "api", side_effect=[
                {"type": "User", "login": "owner"},
                {"name": "image", "package_type": "container", "visibility": "public"}]):
            with self.assertRaisesRegex(ValueError, 'visibility="public"'):
                ghcr.private_packages("owner", ["image"])
