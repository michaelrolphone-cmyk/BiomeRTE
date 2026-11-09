"""Configuration/deployment boundaries plus optional real native lifecycle checks.

Run all native checks on the Mac with ROBOT_VISION_TEST_APP pointing to the
prepared .app. They launch only a private dummy child, disable the network
probe, and never load a LaunchAgent or access the real camera/state.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_macos_app as setup


class LauncherDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="robot-vision-wrapper-")
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.app = self.root / "Applications" / setup.APP_NAME
        self.signature = mock.patch.object(setup, "_verify_signature")
        self.details = mock.patch.object(setup, "_signing_details",
                                         return_value={"Identifier": setup.BUNDLE_ID, "TeamIdentifier": "TESTTEAM",
                                                       "DesignatedRequirement": "stable-test-requirement"})
        self.jobgate = mock.patch.object(setup, "_require_service_unloaded")
        self.jobgate.start()
        self.addCleanup(self.jobgate.stop)
        self.signature.start()
        self.details.start()
        self.addCleanup(self.details.stop)
        self.addCleanup(self.signature.stop)
        self.addCleanup(self.temporary.cleanup)

    def stage(self, name="stage", binary=b"test fixture native binary"):
        stage = self.root / name
        app = stage / setup.APP_NAME
        (app / "Contents/MacOS").mkdir(parents=True)
        stage.chmod(0o700)
        config = setup.make_launcher_config(self.root / "repo", self.state, home=self.root)
        info = setup.bundle_info(self.state / "macos-app/launcher.json")
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        (app / "Contents/MacOS" / setup.EXECUTABLE).write_bytes(binary)
        (app / "Contents/MacOS" / setup.EXECUTABLE).chmod(0o755)
        setup._atomic(stage / "launcher.json", setup._json_bytes(config))
        manifest = {
            "schema_version": 1, "bundle_identifier": setup.BUNDLE_ID,
            "state_dir": str(self.state), "app_path": str(self.app),
            "service_label": setup.SERVICE_LABEL, "source_sha256": "fixture",
            "input_sha256": hashlib.sha256(binary).hexdigest(),
            "bundle_files": setup._bundle_files(app),
            "launcher_config_sha256": setup._sha(stage / "launcher.json"),
            "signing_mode": "apple-issued", "signing_details": {
                "Identifier": setup.BUNDLE_ID, "TeamIdentifier": "TESTTEAM",
                "DesignatedRequirement": "stable-test-requirement"},
            "limitations": [],
        }
        setup._atomic(stage / "manifest.json", setup._json_bytes(manifest))
        setup._atomic(stage / "service.plist",
                      plistlib.dumps(setup.launch_agent_spec(self.state, app_path=self.app)))
        return stage

    def test_plan_has_no_writes_and_directly_supervises_native_main(self):
        spec = setup.launch_agent_spec(self.state, app_path=self.app)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.app.exists())
        self.assertEqual(spec["ProgramArguments"], [
            str(self.app / "Contents/MacOS" / setup.EXECUTABLE),
            "--launcher-config", str(self.state / "macos-app/launcher.json")])
        self.assertEqual(spec["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(spec["ExitTimeOut"], 0)
        self.assertTrue(spec["RunAtLoad"])
        self.assertEqual(spec["Umask"], 0o077)

    def test_required_install_rejects_missing_preparation(self):
        with self.assertRaisesRegex(RuntimeError, "install-prepared"):
            setup.launch_agent_spec(self.state, app_path=self.app, require_installed=True)

    def test_private_config_and_state_binding_are_enforced(self):
        stage = self.stage()
        (stage / "launcher.json").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            setup.validate_prepared(stage)
        (stage / "launcher.json").chmod(0o600)
        manifest = setup._private_json(stage / "manifest.json")
        with self.assertRaisesRegex(ValueError, "destination"):
            setup._validate_payload(manifest, stage / setup.APP_NAME, stage / "launcher.json",
                                    state_dir=self.root / "other-state")

    def test_bundle_and_config_tampering_are_rejected(self):
        stage = self.stage()
        (stage / setup.APP_NAME / "Contents/MacOS" / setup.EXECUTABLE).write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "checksum"):
            setup.validate_prepared(stage)
        stage = self.stage("other")
        (stage / "launcher.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            setup.validate_prepared(stage)

    def test_nonexecutable_native_file_is_rejected(self):
        stage = self.stage()
        (stage / setup.APP_NAME / "Contents/MacOS" / setup.EXECUTABLE).chmod(0o644)
        with self.assertRaisesRegex(ValueError, "not executable"):
            setup.validate_prepared(stage)

    def test_different_bundle_identity_is_never_replaced(self):
        stage = self.stage()
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "org.someone.else", "CFBundleExecutable": "Other"}))
        before = (self.app / "Contents/Info.plist").read_bytes()
        with self.assertRaisesRegex(ValueError, "different application identity"):
            setup.install_prepared(stage)
        self.assertEqual((self.app / "Contents/Info.plist").read_bytes(), before)

    def test_install_is_idempotent_and_does_not_start_jobs(self):
        stage = self.stage()
        first = setup.install_prepared(stage)
        second = setup.install_prepared(stage)
        self.assertFalse(first["jobs_started"])
        self.assertFalse(first["unchanged"])
        self.assertTrue(second["unchanged"])
        setup.launch_agent_spec(self.state, app_path=self.app, require_installed=True)
        self.assertEqual((self.state / "macos-app/launcher.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "macos-app/manifest.json").stat().st_mode & 0o777, 0o600)

    def test_old_python_service_lock_blocks_transition(self):
        stage = self.stage()
        self.state.mkdir(mode=0o700)
        with (self.state / "service.lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "still running"):
                setup.install_prepared(stage)
        self.assertFalse(self.app.exists())
        self.assertFalse((self.state / "macos-app/manifest.json").exists())

    def test_wrapper_lock_blocks_transition(self):
        stage = self.stage()
        self.state.mkdir(mode=0o700)
        with (self.state / "launcher.lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "still running"):
                setup.install_prepared(stage)
        self.assertFalse(self.app.exists())

    def test_failed_update_restores_prior_bundle_and_metadata(self):
        stage = self.stage()
        setup.install_prepared(stage)
        prior_bundle = setup._bundle_files(self.app)
        prior_manifest = (self.state / "macos-app/manifest.json").read_bytes()
        prior_config = (self.state / "macos-app/launcher.json").read_bytes()
        replacement = self.stage("replacement", binary=b"new test fixture native binary")
        real_atomic = setup._atomic
        failed = False

        def fail_once(path, data, mode=0o600):
            nonlocal failed
            if Path(path) == self.state / "macos-app/manifest.json" and not failed:
                failed = True
                raise OSError("simulated metadata failure")
            return real_atomic(path, data, mode)

        with mock.patch.object(setup, "_atomic", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated"):
                setup.install_prepared(replacement)
        self.assertEqual(setup._bundle_files(self.app), prior_bundle)
        self.assertEqual((self.state / "macos-app/manifest.json").read_bytes(), prior_manifest)
        self.assertEqual((self.state / "macos-app/launcher.json").read_bytes(), prior_config)

    def test_loaded_legacy_job_blocks_install_even_without_lock(self):
        stage = self.stage()
        with mock.patch.object(setup, "_require_service_unloaded",
                               side_effect=RuntimeError("vision LaunchAgent is still loaded")):
            with self.assertRaisesRegex(RuntimeError, "still loaded"):
                setup.install_prepared(stage)
        self.assertFalse(self.app.exists())

    def test_designated_requirement_change_is_refused_for_same_team(self):
        stage = self.stage()
        setup.install_prepared(stage)
        prior = setup._bundle_files(self.app)
        replacement = self.stage("identity-change", binary=b"different identity fixture")
        changed = setup._private_json(replacement / "manifest.json")
        changed["signing_details"]["DesignatedRequirement"] = "different-test-requirement"
        setup._atomic(replacement / "manifest.json", setup._json_bytes(changed))
        def details(app):
            requirement = ("different-test-requirement"
                           if (Path(app) / "Contents/MacOS" / setup.EXECUTABLE).read_bytes()
                           == b"different identity fixture" else "stable-test-requirement")
            return {"Identifier": setup.BUNDLE_ID, "TeamIdentifier": "TESTTEAM",
                    "DesignatedRequirement": requirement}
        with mock.patch.object(setup, "_signing_details", side_effect=details):
            with self.assertRaisesRegex(ValueError, "different code-signing identity"):
                setup.install_prepared(replacement)
        self.assertEqual(setup._bundle_files(self.app), prior)

    def test_ad_hoc_commented_implicit_requirement_is_recorded(self):
        self.details.stop()
        result = subprocess.CompletedProcess(
            ["codesign"], 0,
            stdout='# designated => cdhash H"1234567890abcdef"\n',
            stderr="Identifier=" + setup.BUNDLE_ID + "\nSignature=adhoc\nTeamIdentifier=not set\n")
        with mock.patch.object(setup.sys, "platform", "darwin"), \
                mock.patch.object(setup.subprocess, "run", return_value=result):
            details = setup._signing_details(self.app)
        self.assertEqual(details["DesignatedRequirement"], 'cdhash H"1234567890abcdef"')
        self.assertEqual(details["Signature"], "adhoc")

    def test_signing_summary_exposes_only_count_and_types(self):
        summary = setup.signing_summary([
            {"fingerprint": "A" * 40, "certificate_type": "Apple Development"},
            {"fingerprint": "B" * 40, "certificate_type": "Apple Development"},
        ])
        self.assertEqual(summary, {"valid_identity_count": 2, "types": {"Apple Development": 2}})
        self.assertNotIn("fingerprint", json.dumps(summary))


NATIVE_APP = os.environ.get("ROBOT_VISION_TEST_APP")


@unittest.skipUnless(sys.platform == "darwin" and NATIVE_APP,
                     "Set ROBOT_VISION_TEST_APP to run actual prepared-native lifecycle checks on macOS.")
class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="robot-vision-native-test-")
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.script = self.root / "child.py"
        self.script.write_text(
            "import argparse,json,os,pathlib,signal,sys,time\n"
            "p=argparse.ArgumentParser();p.add_argument('--state');p.add_argument('--config');p.add_argument('--parent-pid',type=int);a=p.parse_args()\n"
            "s=pathlib.Path(a.state);cfg=json.loads(pathlib.Path(a.config).read_text())\n"
            "def stop(*args):\n"
            " (s/'signal-received').write_text('yes');time.sleep(0.25)\n"
            " (s/'cleanup-complete').write_text('yes');sys.exit(0)\n"
            "signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)\n"
            "with (s/'starts').open('a') as f:f.write(str(os.getpid())+'\\n')\n"
            "(s/'ready.json').write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid(),'declared_parent':a.parent_pid}))\n"
            "if cfg.get('exit_zero'):sys.exit(0)\n"
            "while True:time.sleep(0.05)\n"
        )
        self.service_config = self.state / "config.json"
        self.service_config.write_text("{}")
        self.config = setup.make_launcher_config(self.root, self.state)
        self.config.update(python=str(Path(sys.executable).resolve()),
                           service_script=str(self.script), show_status_item=False)
        self.config["camera_probe"]["enabled"] = False
        self.config_path = self.root / "launcher.json"
        setup._atomic(self.config_path, setup._json_bytes(self.config))
        self.binary = Path(NATIVE_APP) / "Contents/MacOS" / setup.EXECUTABLE
        self.processes = []
        self.addCleanup(self.cleanup_children)

    def start(self, *extra):
        process = subprocess.Popen([str(self.binary), "--launcher-config", str(self.config_path), *extra],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def wait_for(self, path, process=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process is not None and process.poll() is not None:
                out, err = process.communicate()
                self.fail("Native parent exited before evidence: " + out + err)
            time.sleep(0.05)
        self.fail("Native test evidence was not produced: " + str(path))

    def cleanup_children(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    # Fixture cleanup only, never a production shutdown policy.
                    process.kill()
                    process.wait(timeout=5)
            process.communicate()
        ready = self.state / "ready.json"
        if ready.exists():
            pid = json.loads(ready.read_text())["pid"]
            command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                     capture_output=True, text=True).stdout
            if str(self.script) in command:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.temporary.cleanup()

    def test_actual_native_config_rejects_public_file(self):
        self.config_path.chmod(0o644)
        process = self.start("--validate-config")
        out, err = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 78, out + err)
        self.assertIn("owner-only", err)
        self.assertFalse((self.state / "ready.json").exists())

    def test_sigterm_waits_for_owned_child_cleanup(self):
        process = self.start()
        self.wait_for(self.state / "ready.json", process)
        ready = json.loads((self.state / "ready.json").read_text())
        self.assertEqual(ready["parent"], process.pid)
        self.assertEqual(ready["declared_parent"], process.pid)
        began = time.monotonic()
        process.terminate()
        out, err = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, out + err)
        self.assertGreaterEqual(time.monotonic() - began, 0.20)
        self.assertTrue((self.state / "cleanup-complete").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(ready["pid"], 0)
        status = json.loads((self.state / "macos-app/status.json").read_text())
        self.assertEqual(status["child_state"], "stopped")
        self.assertFalse(status["parent_network_probe"]["child_capture_verified"])

    def test_second_native_parent_cannot_launch_second_child(self):
        first = self.start()
        self.wait_for(self.state / "ready.json", first)
        second = self.start()
        out, err = second.communicate(timeout=15)
        self.assertEqual(second.returncode, 0, out + err)
        self.assertIn("already_running", err)
        self.assertEqual(len((self.state / "starts").read_text().splitlines()), 1)
        self.assertIsNone(first.poll())
        first.terminate()
        first.communicate(timeout=15)

    def test_unexpected_successful_child_exit_is_parent_failure(self):
        self.service_config.write_text('{"exit_zero":true}')
        process = self.start()
        out, err = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0, out + err)
        self.assertIn("unexpected_child_exit", err)
        status = json.loads((self.state / "macos-app/status.json").read_text())
        self.assertEqual(status["child_exit_status"], 0)
        self.assertEqual(status["child_state"], "unexpected_exit")


if __name__ == "__main__":
    unittest.main()
