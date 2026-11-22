"""Independent Docker operation supervision and unresolved-create custody."""
import copy
import json
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from biomerte import docker_services as ds
from tests.test_docker_services import (
    BOOT_EPOCH, GRANT_TOKEN, IMAGE, INCARNATION, WORKER_ID,
    DeferredThread, FakeDocker, RecordingEngine, RunnerCase, manifest,
)

REAL_THREAD = threading.Thread


def wait_for(condition, timeout=3):
    until = time.monotonic() + timeout
    while not condition() and time.monotonic() < until:
        time.sleep(.01)
    return condition()


class DelayedDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()
        self.attempts = 0

    def create(self, *args, **kwargs):
        self.attempts += 1
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError("test did not release Docker")
        return super().create(*args, **kwargs)


class AsyncDockerTests(RunnerCase):
    def begin_delayed(self):
        self.engine = DelayedDocker()
        self.runner.engine = self.engine
        self.prepare()
        threads = mock.patch.object(ds.threading, "Thread", REAL_THREAD)
        threads.start()
        self.addCleanup(threads.stop)
        self.addCleanup(self.finish_jobs)
        self.runner.accept(self.response(), self.clock())
        self.assertTrue(self.engine.entered.wait(2))

    def finish_jobs(self):
        self.engine.release.set()
        self.assertTrue(wait_for(lambda: not self.runner.start_jobs))
        self.assertTrue(wait_for(lambda: not self.runner.cleanup_jobs))

    def test_slow_docker_allows_heartbeats_and_renewal_before_completion(self):
        self.begin_delayed()
        self.assertEqual({}, self.runner.containers)
        self.clock.advance(6)
        self.client.replies.append((0, self.response()))
        self.runner.tick()
        self.assertEqual(1, len(self.client.calls))
        self.assertEqual(116, self.runner.grants[self.value["id"]]["deadline"])
        self.clock.advance(5)  # The first grant deadline passed, but renewal is current.
        self.engine.release.set()
        self.assertTrue(wait_for(lambda: not self.runner.start_jobs))
        self.assertEqual(1, self.engine.attempts)
        self.assertTrue(self.runner.advertisements()[0]["healthy"])

    def test_expiry_watchdog_advances_while_docker_call_is_blocked(self):
        self.begin_delayed()
        watchdog = REAL_THREAD(target=self.runner.watchdog)
        watchdog.start()
        try:
            self.clock.advance(11)
            self.assertTrue(wait_for(lambda: not self.runner.grants))
            self.assertFalse(self.engine.release.is_set())
        finally:
            self.runner.stop_event.set()
            watchdog.join(2)
            self.engine.release.set()
        self.assertTrue(wait_for(lambda: not self.runner.start_jobs))
        self.assertEqual([], self.engine.created)
        self.assertEqual({}, self.runner.containers)

    def test_supersession_does_not_launch_second_job_before_old_one_settles(self):
        self.begin_delayed()
        replacement = self.grant(grant_token="replacement-token")
        self.runner.accept(self.response(replacement), self.clock())
        self.runner.accept(self.response(replacement), self.clock())
        self.assertEqual(1, self.engine.attempts)
        self.engine.release.set()
        self.assertTrue(wait_for(lambda: not self.runner.start_jobs))
        self.assertEqual([], self.engine.created)
        self.runner.accept(self.response(replacement), self.clock())
        self.assertTrue(wait_for(lambda: not self.runner.start_jobs))
        self.assertEqual(2, self.engine.attempts)
        self.assertEqual("replacement-token", self.engine.created[0]["grant"]["grant_token"])

    def test_full_manifest_binding_change_replaces_child_even_if_token_is_reused(self):
        self.prepare()
        self.runner.accept(self.response(), self.clock())
        old = self.runner.containers[self.value["id"]]
        changed = manifest(command=["new-command"])
        self.runner.adopt_manifests({"manifests": [changed],
                                    "configured_revisions": {changed["id"]: changed["revision"]}})
        self.runner.advertisements()
        grant = self.grant(manifest_revision=changed["revision"])
        self.runner.accept(self.response(grant), self.clock())
        self.assertIn(old, self.engine.removed)
        self.assertNotEqual(old, self.runner.containers[self.value["id"]])

    def test_unanswered_create_keeps_same_reference_until_late_container_is_removed(self):
        self.prepare()
        name = ds.container_name(self.value, WORKER_ID, INCARNATION, self.grant())
        error = TimeoutError("create response lost")
        error.container_id, error.creation_uncertain = name, True
        self.engine.start_error = error
        self.runner.accept(self.response(), self.clock())
        self.assertEqual(name, self.runner.pending_cleanup[self.value["id"]]["container"])
        self.assertEqual(name, self.runner.state.children()[self.value["id"]]["container"])
        self.assertIsNone(self.runner._health(self.value["id"]))
        # Absence in one inspect is not proof that the old create cannot finish.
        self.runner.accept(self.response(), self.clock())
        self.assertEqual([], self.engine.created)
        self.assertFalse(self.runner.advertisements()[0]["dependencies_ready"])
        self.engine.add_child(name, running=False)
        self.runner.expire()
        self.assertIn(name, self.engine.removed)
        self.assertEqual({}, self.runner.pending_cleanup)
        self.assertEqual({}, self.runner.state.children())
        self.engine.start_error = None
        self.runner.advertisements()
        self.runner.accept(self.response(), self.clock())
        self.assertEqual(1, len(self.engine.created))

    def test_missing_create_eventually_unblocks_and_inventory_reaps_late_child(self):
        self.prepare()
        name = ds.container_name(self.value, WORKER_ID, INCARNATION, self.grant())
        error = TimeoutError("unanswered create")
        error.container_id, error.creation_uncertain = name, True
        self.engine.start_error = error
        self.runner.accept(self.response(), self.clock())
        self.runner.expire()
        self.clock.advance(ds.INVENTORY_INTERVAL_S + 1)
        self.runner.expire()
        self.assertFalse(self.runner.pending_cleanup)
        self.engine.start_error = None
        self.runner.advertisements()
        self.runner.accept(self.response(self.grant(grant_token="next")), self.clock())
        child = self.runner.containers[self.value["id"]]
        self.engine.add_child(name, running=False)
        self.engine.children[name]["Config"]["Labels"][ds.SERVICE] = self.value["id"]
        self.clock.advance(ds.INVENTORY_INTERVAL_S)
        self.runner.advertisements()
        self.assertIn(name, self.engine.removed)
        self.assertIn(child, self.engine.children)

    def test_pending_create_custody_survives_supervisor_restart(self):
        self.prepare()
        name = ds.container_name(self.value, WORKER_ID, INCARNATION, self.grant())
        self.runner.state.save_child(self.value["id"], name, True)
        replacement = ds.Runner(self.runner.config, engine=self.engine,
                                client=self.client, clock=self.clock)
        replacement.load_manifests()
        self.assertFalse(replacement.advertisements()[0]["dependencies_ready"])
        self.assertIn(self.value["id"], replacement.pending_cleanup)
        self.engine.add_child(name, running=False)
        replacement.expire()
        self.assertIn(name, self.engine.removed)
        self.assertEqual({}, replacement.pending_cleanup)
        self.assertTrue(replacement.advertisements()[0]["dependencies_ready"])

    def test_multiple_orphans_of_one_service_all_remain_in_cleanup_custody(self):
        for child in ("orphan-one", "orphan-two", "orphan-three"):
            self.engine.add_child(child)
            self.engine.children[child]["Config"]["Labels"][ds.SERVICE] = self.value["id"]
        with mock.patch.object(ds.threading, "Thread", DeferredThread):
            report = self.prepare()[0]
            self.assertFalse(report["dependencies_ready"])
            self.assertEqual(3, len(self.runner.pending_cleanup))
            self.assertEqual(3, len(self.runner.cleanup_jobs))
            for job in list(self.runner.cleanup_jobs.values()):
                job.finish()
        self.assertEqual({"orphan-one", "orphan-two", "orphan-three"}, set(self.engine.removed))
        self.assertEqual({}, self.runner.pending_cleanup)
        self.assertTrue(self.runner.advertisements()[0]["dependencies_ready"])

    def test_startup_resolves_retained_name_to_id_before_orphan_removal(self):
        name = ds.container_name(self.value, WORKER_ID, INCARNATION, self.grant())
        self.runner.state.save_child(self.value["id"], name, True)
        self.engine.add_child("old-container-id")
        self.engine.children["old-container-id"]["Name"] = "/" + name
        self.engine.children["old-container-id"]["Config"]["Labels"][ds.SERVICE] = self.value["id"]
        replacement = ds.Runner(self.runner.config, engine=self.engine,
                                client=self.client, clock=self.clock)
        replacement.load_manifests()
        self.assertTrue(replacement.advertisements()[0]["dependencies_ready"])
        self.assertEqual({}, replacement.pending_cleanup)
        self.assertEqual({}, replacement.state.children())
        self.assertEqual(["old-container-id"], self.engine.removed)

    def test_post_create_persistence_failure_cleans_known_running_child(self):
        self.prepare()
        save = self.runner.state.save_child
        calls = []
        def fail_final(service_id, container, uncertain):
            calls.append(container)
            if len(calls) == 3:
                raise OSError("local checkpoint disk unavailable after Docker start")
            return save(service_id, container, uncertain)
        with mock.patch.object(self.runner.state, "save_child", side_effect=fail_final):
            self.runner.accept(self.response(), self.clock())
        child = self.engine.created[0]["id"]
        self.assertIn(child, self.engine.removed)
        self.assertEqual({}, self.runner.containers)
        self.assertEqual({}, self.runner.pending_cleanup)
        self.assertEqual({}, self.runner.state.children())

    def test_cleanup_keeps_memory_custody_when_persistence_and_remove_both_fail(self):
        self.prepare()
        self.runner.accept(self.response(), self.clock())
        child = self.runner.containers[self.value["id"]]
        with mock.patch.object(self.runner.state, "save_child", side_effect=OSError("disk unavailable")), \
                mock.patch.object(self.engine, "remove_managed", side_effect=OSError("Docker unavailable")):
            self.runner.stop_service(self.value["id"])
            self.assertEqual({}, self.runner.grants)
            self.assertEqual(child, self.runner.pending_cleanup[self.value["id"]]["container"])
            self.assertIn(child, self.engine.children)
            self.assertIsNone(self.runner._health(self.value["id"]))
        self.runner.expire()
        self.assertIn(child, self.engine.removed)
        self.assertEqual({}, self.runner.pending_cleanup)

    def test_slow_precreate_durability_does_not_block_expiry(self):
        self.prepare()
        entered, release = threading.Event(), threading.Event()
        save = self.runner.state.save_child
        first = [True]
        def blocked(service_id, container, uncertain):
            if first[0]:
                first[0] = False
                entered.set()
                release.wait(3)
            return save(service_id, container, uncertain)
        with mock.patch.object(self.runner.state, "save_child", side_effect=blocked), \
                mock.patch.object(ds.threading, "Thread", REAL_THREAD):
            self.runner.accept(self.response(), self.clock())
            self.assertTrue(entered.wait(2))
            watchdog = REAL_THREAD(target=self.runner.watchdog)
            watchdog.start()
            try:
                self.clock.advance(11)
                self.assertTrue(wait_for(lambda: not self.runner.grants))
                self.assertFalse(release.is_set())
                self.assertEqual([], self.engine.created)
            finally:
                self.runner.stop_event.set()
                watchdog.join(2)
                release.set()
                self.assertTrue(wait_for(lambda: not self.runner.start_jobs))
                self.assertTrue(wait_for(lambda: not self.runner.cleanup_jobs))

    def test_blocked_removal_does_not_hold_grant_lock_or_retain_local_admission(self):
        self.prepare()
        self.runner.accept(self.response(), self.clock())
        entered, release = threading.Event(), threading.Event()
        remove = self.engine.remove_managed
        def blocked(container, worker):
            entered.set()
            release.wait(3)
            remove(container, worker)
        with mock.patch.object(self.engine, "remove_managed", side_effect=blocked), \
                mock.patch.object(ds.threading, "Thread", REAL_THREAD):
            self.runner.stop_service(self.value["id"])
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual({}, self.runner.grants)
                self.assertTrue(self.runner.lock.acquire(blocking=False))
                self.runner.lock.release()
                self.assertIsNone(self.runner._health(self.value["id"]))
                replacement = self.grant(grant_token="next-token")
                self.runner.accept(self.response(replacement), self.clock())
                self.assertEqual(1, len(self.engine.created))
            finally:
                release.set()
                self.assertTrue(wait_for(lambda: not self.runner.cleanup_jobs))


class DockerTransportTests(unittest.TestCase):
    def test_expiry_between_create_and_start_never_sends_start(self):
        engine = RecordingEngine()
        admitted = [True, False]
        with self.assertRaises(ds.DockerError):
            engine.create(manifest(), IMAGE, WORKER_ID, INCARNATION,
                          {"boot_epoch": BOOT_EPOCH, "grant_token": GRANT_TOKEN},
                          admit=lambda: admitted.pop(0))
        self.assertFalse(any(path.endswith("/start") for _, path, _ in engine.calls))
        self.assertFalse(engine.exists)

    def test_lost_create_reply_preserves_exact_deterministic_name_and_does_not_start(self):
        class LostCreate(RecordingEngine):
            def request(self, method, path, value=None, **options):
                if method == "POST" and "/containers/create?" in path:
                    self.calls.append((method, path, copy.deepcopy(value)))
                    raise TimeoutError("response lost")
                return super().request(method, path, value, **options)
        engine = LostCreate()
        grant = {"boot_epoch": BOOT_EPOCH, "grant_token": GRANT_TOKEN}
        with self.assertRaises(TimeoutError) as raised:
            engine.create(manifest(), IMAGE, WORKER_ID, INCARNATION, grant)
        error = raised.exception
        self.assertTrue(error.creation_uncertain)
        name = parse_qs(urlsplit(engine.calls[0][1]).query)["name"][0]
        self.assertEqual(name, error.container_id)
        self.assertEqual(ds.container_name(manifest(), WORKER_ID, INCARNATION, grant), name)
        self.assertFalse(any(path.endswith("/start") for _, path, _ in engine.calls))

    def test_operation_timeout_is_configurable_and_excludes_fast_inspection_budget(self):
        class Timeouts(RecordingEngine):
            def __init__(self):
                super().__init__()
                self.operation_timeout = 47
                self.timeouts = []
            def request(self, method, path, value=None, **options):
                self.timeouts.append((path, options.get("timeout", "default")))
                return super().request(method, path, value, **options)
        engine = Timeouts()
        engine.create(manifest(), IMAGE, WORKER_ID, INCARNATION,
                      {"boot_epoch": BOOT_EPOCH, "grant_token": GRANT_TOKEN})
        self.assertEqual([47, 47], [timeout for _, timeout in engine.timeouts])

    def test_docker_operation_timeout_rejects_nonfinite_or_nonpositive_configuration(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            for timeout in (0, -1, True, float("inf"), float("nan")):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(ValueError):
                        ds.Runner({"worker_id": WORKER_ID, "docker_request_timeout_s": timeout,
                                   "state_file": os.path.join(directory, "state.sqlite")})


if __name__ == "__main__":
    unittest.main()
