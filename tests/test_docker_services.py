"""Behavioral qualification for Docker service admission and owner expiry.

These tests exercise the runner without a Docker daemon or physical device.
Fake time makes delayed replies and exact lease boundaries reproducible.
"""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from biomerte import docker_services as ds


IMAGE = "sha256:" + "1" * 64
WORKER_ID = "test-worker"
INCARNATION = "test-incarnation"
BOOT_EPOCH = "b" * 32
GRANT_TOKEN = "a" * 32


def manifest(**changes):
    value = {
        "id": "test-service",
        "images": {"linux/arm64": IMAGE},
        "effect_policy": "stateless",
        "state_policy": "stateless",
        "lease_ttl_s": 10,
        "command": ["test-service"],
    }
    value.update(changes)
    value["revision"] = ds.revision(value)
    return value


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeDocker:
    def __init__(self):
        self.host = {"Os": "linux", "Arch": "aarch64"}
        self.images = {IMAGE: {"Id": IMAGE, "Os": "linux", "Architecture": "arm64"}}
        self.children = {}
        self.created = []
        self.removed = []
        self.pulled = []
        self.start_error = None
        self.after_create = None

    def version(self):
        return dict(self.host)

    def image(self, image_ref):
        return self.images.get(image_ref)

    def pull(self, image_ref):
        self.pulled.append(image_ref)
        self.images[image_ref] = {"Id": image_ref, "Os": "linux", "Architecture": "arm64"}

    def add_child(self, container, worker=WORKER_ID, managed=True, running=True):
        self.children[container] = {
            "Id": container,
            "State": {"Running": running},
            "Config": {"Labels": {
                ds.MANAGED: "true" if managed else "false",
                ds.WORKER: worker,
            }},
        }

    def containers(self, worker_id):
        return [
            {"Id": key} for key, value in self.children.items()
            if value["Config"]["Labels"].get(ds.MANAGED) == "true"
            and value["Config"]["Labels"].get(ds.WORKER) == worker_id
        ]

    def inspect(self, container):
        value = self.children.get(container)
        return copy.deepcopy(value) if value is not None else None

    def remove_managed(self, container, worker_id):
        value = self.children.get(container)
        if value is None:
            return
        labels = value["Config"]["Labels"]
        if labels.get(ds.MANAGED) != "true" or labels.get(ds.WORKER) != worker_id:
            raise ds.DockerError("foreign container")
        self.removed.append(container)
        del self.children[container]

    def create(self, value, image_ref, worker, incarnation, grant, overrides=None):
        if self.start_error is not None:
            raise self.start_error
        container = "managed-child-" + str(len(self.created) + 1)
        self.created.append({
            "id": container, "manifest": copy.deepcopy(value),
            "image_ref": image_ref, "worker": worker,
            "incarnation": incarnation, "grant": copy.deepcopy(grant),
        })
        self.add_child(container, worker)
        if self.after_create is not None:
            self.after_create()
        return container


class FakeCoordinator:
    def __init__(self, clock):
        self.clock = clock
        self.replies = []
        self.calls = []

    def call(self, operation, value):
        self.calls.append((operation, copy.deepcopy(value)))
        delay, result = self.replies.pop(0)
        self.clock.advance(delay)
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)


class JsonReply:
    def __init__(self, value, before_read=None):
        self.value = value
        self.before_read = before_read

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self):
        if self.before_read:
            self.before_read()
        return json.dumps(self.value).encode("utf-8")

    def getcode(self):
        return 200


class InlineThread:
    def __init__(self, target, args=()):
        self.target, self.args = target, args
        self.daemon = False

    def start(self):
        self.target(*self.args)


class DeferredThread(InlineThread):
    def start(self):
        pass

    def finish(self):
        self.target(*self.args)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = Clock()
        self.engine = FakeDocker()
        self.client = FakeCoordinator(self.clock)
        self.manifest_path = os.path.join(self.directory.name, "service.json")
        self.value = manifest()
        self.write_manifest(self.value)
        self.runner = ds.Runner({
            "worker_id": WORKER_ID,
            "test_incarnation": INCARNATION,
            "manifest_files": [self.manifest_path],
            "state_file": os.path.join(self.directory.name, "state.sqlite"),
        }, engine=self.engine, client=self.client, clock=self.clock)

    def write_manifest(self, value):
        with open(self.manifest_path, "w") as out:
            json.dump(value, out)

    def prepare(self):
        self.runner.load_manifests()
        return self.runner.advertisements()

    def grant(self, **changes):
        value = {
            "service_id": self.value["id"],
            "manifest_revision": self.value["revision"],
            "worker_id": WORKER_ID,
            "worker_incarnation": INCARNATION,
            "grant_token": GRANT_TOKEN,
            "boot_epoch": BOOT_EPOCH,
            "lease_ttl_s": 10,
            "state": "active",
        }
        value.update(changes)
        return value

    def response(self, grant=None, **changes):
        value = {"boot_epoch": BOOT_EPOCH, "assignments": [grant or self.grant()]}
        value.update(changes)
        return value

    def start(self):
        self.prepare()
        self.runner.accept(self.response(), self.clock())
        self.assertIn(self.value["id"], self.runner.containers)
        return self.runner.containers[self.value["id"]]


class ManifestAdmissionTests(unittest.TestCase):
    def test_revision_detects_changed_command_before_admission(self):
        value = manifest()
        value["command"] = ["changed-after-approval"]
        with self.assertRaises(ValueError):
            ds.validate_manifest(value)

    def test_local_authority_and_unfenced_effects_are_rejected(self):
        invalid = [
            manifest(state_policy="local"),
            manifest(state_policy="local_sqlite"),
            manifest(effect_policy="externally_fenced"),
            manifest(effect_policy="gateway_fenced", fence_hook={"url": ""}),
            manifest(mounts=[{"source": "/data", "target": "/state", "read_only": False}]),
            manifest(restart_policy="always"),
            manifest(images={"linux/arm64": "python:latest"}),
            manifest(command="unsafe shell text"),
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ds.validate_manifest(value)

    def test_nonfinite_timing_cannot_create_an_unexpiring_owner(self):
        for key in ("lease_ttl_s", "worker_ttl_s", "startup_grace_s"):
            for value in (float("nan"), float("inf"), -float("inf"), 0, -1, True):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        ds.validate_manifest(manifest(**{key: value}))

    def test_malformed_identity_and_platform_have_validation_errors(self):
        for value in (
            manifest(id=None), manifest(id=3), manifest(images={3: IMAGE}),
            manifest(mounts=[None]), manifest(command=[False]),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ds.validate_manifest(value)


class LeaseAndFailureTests(RunnerCase):
    def test_delayed_heartbeat_uses_send_time_as_deadline(self):
        self.client.replies.append((3, self.response()))
        self.runner.tick()
        self.assertEqual(110, self.runner.grants[self.value["id"]]["deadline"])
        self.assertEqual(103, self.clock())
        self.clock.now = 109.999
        self.runner.expire()
        self.assertIn(self.value["id"], self.runner.containers)
        self.clock.now = 110
        self.runner.expire()
        self.assertNotIn(self.value["id"], self.runner.containers)

    def test_response_at_exact_deadline_never_starts_service(self):
        self.client.replies.append((10, self.response()))
        self.runner.tick()
        self.assertEqual([], self.engine.created)
        self.assertEqual({}, self.runner.grants)

    def test_response_after_deadline_never_starts_service(self):
        self.client.replies.append((12, self.response()))
        self.runner.tick()
        self.assertEqual([], self.engine.created)
        self.assertEqual({}, self.runner.containers)

    def test_unreachable_coordinator_cannot_extend_the_old_lease(self):
        child = self.start()
        self.client.replies.append((3, RuntimeError("unreachable")))
        self.clock.now = 105
        with self.assertRaises(RuntimeError):
            self.runner.tick()
        self.assertEqual(110, self.runner.grants[self.value["id"]]["deadline"])
        self.clock.now = 110
        self.runner.expire()
        self.assertEqual([child], self.engine.removed)
        self.assertEqual({}, self.runner.containers)

    def test_mismatched_or_incomplete_bindings_never_start(self):
        self.prepare()
        cases = [
            ("worker_id", "another-worker"),
            ("worker_incarnation", "old-incarnation"),
            ("manifest_revision", "0" * 64),
            ("boot_epoch", "c" * 32),
            ("grant_token", ""),
            ("boot_epoch", ""),
            ("grant_token", 123),
            ("boot_epoch", 123),
        ]
        for key, wrong in cases:
            with self.subTest(key=key, wrong=wrong):
                value = self.grant(**{key: wrong})
                before = len(self.engine.created)
                self.runner.accept(self.response(value), self.clock())
                self.assertEqual(before, len(self.engine.created))
        for key in ("worker_id", "worker_incarnation", "manifest_revision", "grant_token", "boot_epoch"):
            with self.subTest(missing=key):
                value = self.grant()
                del value[key]
                before = len(self.engine.created)
                self.runner.accept(self.response(value), self.clock())
                self.assertEqual(before, len(self.engine.created))


    def test_response_boot_identity_must_be_present_and_nonempty_text(self):
        self.prepare()
        for boot in (None, "", 123, False):
            with self.subTest(boot=boot):
                before = len(self.engine.created)
                response = self.response(self.grant(boot_epoch=boot), boot_epoch=boot)
                self.runner.accept(response, self.clock())
                self.assertEqual(before, len(self.engine.created))
        response = self.response()
        del response["boot_epoch"]
        before = len(self.engine.created)
        self.runner.accept(response, self.clock())
        self.assertEqual(before, len(self.engine.created))

    def test_nonfinite_and_boolean_grant_lifetimes_never_start(self):
        self.prepare()
        for ttl in (float("nan"), float("inf"), 0, -1, True):
            with self.subTest(ttl=ttl):
                try:
                    self.runner.accept(self.response(self.grant(lease_ttl_s=ttl)), self.clock())
                except ValueError:
                    pass
                self.assertEqual([], self.engine.created)

    def test_wrong_manifest_revision_withdraws_an_existing_child(self):
        child = self.start()
        self.runner.accept(self.response(self.grant(manifest_revision="0" * 64)), self.clock())
        self.assertIn(child, self.engine.removed)
        self.assertEqual({}, self.runner.containers)

    def test_replacement_token_stops_the_old_child_before_starting_new(self):
        old = self.start()
        self.engine.add_child("another-workers-child", worker="other-worker")
        self.runner.accept(self.response(self.grant(grant_token="d" * 32)), self.clock())
        self.assertEqual([old], self.engine.removed)
        self.assertIn("another-workers-child", self.engine.children)
        self.assertEqual("d" * 32, self.engine.created[-1]["grant"]["grant_token"])
        self.assertNotEqual(old, self.runner.containers[self.value["id"]])

    def test_coordinator_boot_change_replaces_prior_binding(self):
        old = self.start()
        new_boot = "e" * 32
        self.runner.accept(self.response(self.grant(boot_epoch=new_boot, grant_token="f" * 32),
                                         boot_epoch=new_boot), self.clock())
        self.assertEqual([old], self.engine.removed)
        self.assertEqual(new_boot, self.runner.grants[self.value["id"]]["boot_epoch"])

    def test_revocation_removes_only_this_workers_managed_child(self):
        old = self.start()
        self.engine.add_child("unmanaged", managed=False)
        self.engine.add_child("foreign-managed", worker="different-worker")
        self.runner.accept(self.response(assignments=[]), self.clock())
        self.assertEqual([old], self.engine.removed)
        self.assertEqual({"unmanaged", "foreign-managed"}, set(self.engine.children))
        self.assertEqual({}, self.runner.grants)

    def test_new_incarnation_cleanup_preserves_unrelated_containers(self):
        self.engine.add_child("our-orphan")
        self.engine.add_child("unmanaged", managed=False)
        self.engine.add_child("foreign-managed", worker="different-worker")
        self.prepare()
        self.assertEqual(["our-orphan"], self.engine.removed)
        self.assertEqual({"unmanaged", "foreign-managed"}, set(self.engine.children))

    def test_late_docker_start_is_cleaned_before_becoming_ready(self):
        self.prepare()
        self.engine.after_create = lambda: self.clock.advance(11)
        self.runner.accept(self.response(), self.clock())
        self.assertEqual(1, len(self.engine.created))
        self.assertEqual([self.engine.created[0]["id"]], self.engine.removed)
        self.assertEqual({}, self.runner.containers)
        self.assertEqual({}, self.runner.grants)

    def test_failed_start_never_reports_a_healthy_running_service(self):
        self.prepare()
        self.engine.start_error = ds.DockerError("start failed")
        self.runner.accept(self.response(), self.clock())
        self.assertEqual({}, self.runner.containers)
        entries = self.runner.advertisements()
        self.assertIsNot(entries[0]["healthy"], True)

    def test_expiry_snapshot_cannot_kill_a_concurrently_renewed_grant(self):
        child = self.start()
        self.clock.now = 110
        original = self.runner.stop_service

        def renew_before_custody(service_id, expired_only=False):
            # Represents a completed renewal after expire's snapshot, before
            # stop_service acquires the removal lock.
            self.runner.grants[service_id]["deadline"] = 120
            return original(service_id, expired_only=expired_only)

        with mock.patch.object(self.runner, "stop_service", side_effect=renew_before_custody):
            self.runner.expire()
        self.assertEqual(child, self.runner.containers[self.value["id"]])
        self.assertEqual([], self.engine.removed)



class ManifestPropagationTests(RunnerCase):
    def restarted_runner(self):
        return ds.Runner({
            "worker_id": WORKER_ID,
            "test_incarnation": "a-new-incarnation",
            "manifest_files": [self.manifest_path],
            "state_file": os.path.join(self.directory.name, "state.sqlite"),
        }, engine=self.engine, client=self.client, clock=self.clock)

    def test_new_coordinator_service_is_prepared_before_start_and_survives_restart(self):
        added = manifest(id="future-service", command=["future-service"])
        grant = self.grant(service_id=added["id"], manifest_revision=added["revision"])
        self.client.replies.append((0, self.response(grant, manifests=[added])))
        self.runner.tick()
        self.assertEqual([], self.engine.created, "A propagated manifest bypassed preparation")
        self.assertIn(added["id"], self.runner.manifests)
        self.assertFalse(self.runner.prepared[added["id"]])

        self.client.replies.append((0, self.response(grant)))
        self.runner.tick()
        sent = self.client.calls[-1][1]["services"]
        prepared = next(item for item in sent if item["manifest"]["id"] == added["id"])
        self.assertTrue(prepared["prepared"])
        self.assertTrue(prepared["dependencies_ready"])
        self.assertEqual(added["revision"], self.engine.created[-1]["manifest"]["revision"])

        restarted = self.restarted_runner()
        restarted.load_manifests()
        self.assertEqual(added, restarted.manifests[added["id"]])

    def test_unsolicited_revision_cannot_replace_an_approved_running_service(self):
        old = self.start()
        replacement = manifest(command=["unapproved-command"])
        self.client.replies.append((0, self.response(manifests=[replacement])))
        self.runner.tick()
        self.assertEqual(self.value, self.runner.manifests[self.value["id"]])
        self.assertEqual(old, self.runner.containers[self.value["id"]])
        self.assertEqual(1, len(self.engine.created))
        restarted = self.restarted_runner()
        restarted.load_manifests()
        self.assertEqual(self.value, restarted.manifests[self.value["id"]])

    def test_configured_revision_replaces_old_child_only_after_new_preparation(self):
        old = self.start()
        replacement = manifest(command=["approved-new-command"])
        grant = self.grant(manifest_revision=replacement["revision"], grant_token="d" * 32)
        self.client.replies.append((0, self.response(
            grant, manifests=[replacement],
            configured_revisions={replacement["id"]: replacement["revision"]})))
        self.runner.tick()
        self.assertEqual([old], self.engine.removed)
        self.assertEqual({}, self.runner.containers)
        self.assertEqual(1, len(self.engine.created))
        self.assertFalse(self.runner.prepared[replacement["id"]])

        self.client.replies.append((0, self.response(grant)))
        self.runner.tick()
        self.assertEqual(2, len(self.engine.created))
        self.assertEqual(["approved-new-command"], self.engine.created[-1]["manifest"]["command"])
        self.assertEqual(replacement["revision"], self.engine.created[-1]["grant"]["manifest_revision"])

    def test_cached_approved_revision_is_not_downgraded_by_an_old_seed_file(self):
        self.prepare()
        replacement = manifest(command=["approved-new-command"])
        self.client.replies.append((0, self.response(
            assignments=[], manifests=[replacement],
            configured_revisions={replacement["id"]: replacement["revision"]})))
        self.runner.tick()
        # The original local seed remains unchanged while coordinator state advances.
        with open(self.manifest_path) as source:
            self.assertEqual(self.value, json.load(source))
        restarted = self.restarted_runner()
        restarted.load_manifests()
        self.assertEqual(replacement, restarted.manifests[replacement["id"]])

    def test_invalid_propagated_manifest_does_not_corrupt_cached_revision(self):
        self.prepare()
        replacement = manifest(command=["approved-new-command"])
        replacement["command"] = ["changed-without-revision"]
        self.client.replies.append((0, self.response(
            assignments=[], manifests=[replacement],
            configured_revisions={replacement["id"]: replacement["revision"]})))
        with self.assertRaises(ValueError):
            self.runner.tick()
        restarted = self.restarted_runner()
        restarted.load_manifests()
        self.assertEqual(self.value, restarted.manifests[self.value["id"]])


class ImagePreparationTests(RunnerCase):
    def test_wrong_arch_image_never_advertises_prepared(self):
        self.engine.images[IMAGE]["Architecture"] = "amd64"
        entry = self.prepare()[0]
        self.assertFalse(entry["prepared"])
        self.assertFalse(self.runner.prepared[self.value["id"]])
        self.runner.accept(self.response(), self.clock())
        self.assertEqual([], self.engine.created)

    def test_nonlinux_image_is_not_admitted_on_linux_engine(self):
        self.engine.images[IMAGE]["Os"] = "windows"
        entry = self.prepare()[0]
        self.assertFalse(entry["prepared"])

    def test_unsupported_host_platform_cannot_receive_this_service(self):
        self.engine.host["Arch"] = "armv7l"
        entry = self.prepare()[0]
        self.assertEqual("linux/arm/v7", entry["platform"])
        self.assertFalse(entry["prepared"])
        self.assertEqual([], self.engine.pulled)


    def test_transient_registry_failure_retries_after_deadline_then_prepares(self):
        image_ref = "example.invalid/service@sha256:" + "3" * 64
        self.value = manifest(images={"linux/arm64": image_ref})
        self.write_manifest(self.value)
        self.runner.config["image_retry_s"] = 5
        attempts = []

        def flaky_pull(reference):
            attempts.append((self.clock(), reference))
            if len(attempts) == 1:
                raise ds.DockerError("temporary registry outage")
            self.engine.images[reference] = {
                "Id": reference, "Os": "linux", "Architecture": "arm64"}

        with mock.patch.object(self.engine, "pull", side_effect=flaky_pull), \
                mock.patch.object(ds.threading, "Thread", InlineThread):
            first = self.prepare()[0]
            self.assertFalse(first["prepared"])
            self.runner.accept(self.response(), self.clock())
            self.assertEqual([], self.engine.created)
            self.runner.advertisements()
            self.clock.now = 104.999
            self.runner.advertisements()
            self.assertEqual([(100.0, image_ref)], attempts)
            self.clock.now = 105
            self.runner.advertisements()
            self.assertEqual([(100.0, image_ref), (105, image_ref)], attempts)
            ready = self.runner.advertisements()[0]
            self.assertTrue(ready["prepared"])
            self.assertTrue(ready["dependencies_ready"])
            self.runner.accept(self.response(), self.clock())
            self.assertEqual(image_ref, self.engine.created[-1]["image_ref"])

    def test_pull_preparation_does_not_admit_image_before_completion(self):
        image_ref = "example.invalid/service@sha256:" + "2" * 64
        self.value = manifest(images={"linux/arm64": image_ref})
        self.write_manifest(self.value)
        with mock.patch.object(ds.threading, "Thread", DeferredThread):
            entry = self.prepare()[0]
        self.assertFalse(entry["prepared"])
        self.assertFalse(self.runner.prepared[self.value["id"]])
        self.runner.accept(self.response(), self.clock())
        self.assertEqual([], self.engine.created)
        self.runner.pulls[image_ref].finish()
        self.assertTrue(self.runner.advertisements()[0]["prepared"])


class ExternalFencingTests(RunnerCase):
    def setUp(self):
        super().setUp()
        self.value = manifest(effect_policy="externally_fenced",
                              fence_hook={"url": "https://sink.invalid/fence"})
        self.write_manifest(self.value)
        self.prepare()

    def grant(self, **changes):
        value = super().grant(**changes)
        value.setdefault("effect_ready", value["state"] in ("active", "starting"))
        return value

    def acknowledgement(self, **changes):
        value = {"fenced": True, "boot_epoch": BOOT_EPOCH, "grant_token": GRANT_TOKEN}
        value.update(changes)
        return value

    def test_fence_ack_alone_cannot_start_before_coordinator_ready(self):
        pending = self.grant(state="fencing")
        with mock.patch.object(ds.threading, "Thread", InlineThread), \
                mock.patch.object(ds, "urlopen", return_value=JsonReply(self.acknowledgement())):
            self.runner.accept(self.response(pending), self.clock())
        self.assertEqual([], self.engine.created)
        self.assertEqual(GRANT_TOKEN, self.runner.advertisements()[0]["fence_ack"]["grant_token"])
        self.runner.accept(self.response(pending), self.clock())
        self.assertEqual([], self.engine.created)
        self.runner.accept(self.response(self.grant(state="active")), self.clock())
        self.assertEqual(1, len(self.engine.created))


    def test_starting_with_coordinator_readiness_and_exact_fence_ack_can_become_healthy(self):
        pending = self.grant(state="fencing", effect_ready=False)
        with mock.patch.object(ds.threading, "Thread", InlineThread), \
                mock.patch.object(ds, "urlopen", return_value=JsonReply(self.acknowledgement())):
            self.runner.accept(self.response(pending), self.clock())
        self.assertEqual([], self.engine.created)
        self.runner.accept(self.response(self.grant(state="starting", effect_ready=True)), self.clock())
        self.assertEqual(1, len(self.engine.created))
        child = self.runner.containers[self.value["id"]]
        self.assertTrue(self.runner.advertisements()[0]["healthy"])
        self.runner.accept(self.response(self.grant(state="active", effect_ready=True)), self.clock())
        self.assertEqual(child, self.runner.containers[self.value["id"]])

    def test_exact_sink_ack_still_requires_explicit_coordinator_effect_readiness(self):
        pending = self.grant(state="fencing", effect_ready=False)
        with mock.patch.object(ds.threading, "Thread", InlineThread), \
                mock.patch.object(ds, "urlopen", return_value=JsonReply(self.acknowledgement())):
            self.runner.accept(self.response(pending), self.clock())
        for phase in ("starting", "active"):
            for ready in (False, None, 1, "true"):
                with self.subTest(phase=phase, effect_ready=ready):
                    grant = self.grant(state=phase, effect_ready=ready)
                    if ready is None:
                        del grant["effect_ready"]
                    before = len(self.engine.created)
                    self.runner.accept(self.response(grant), self.clock())
                    self.assertEqual(before, len(self.engine.created))
        self.runner.accept(self.response(self.grant(state="fencing", effect_ready=True)), self.clock())
        self.assertEqual([], self.engine.created)

    def test_active_assignment_without_sink_ack_remains_stopped(self):
        with mock.patch.object(ds.threading, "Thread", DeferredThread):
            self.runner.accept(self.response(), self.clock())
        self.assertEqual([], self.engine.created)
        self.assertNotIn(self.value["id"], self.runner.fence_acks)

    def test_wrong_fence_proof_never_unlocks_service(self):
        for ack in (
            self.acknowledgement(fenced=False),
            self.acknowledgement(grant_token="d" * 32),
            self.acknowledgement(boot_epoch="c" * 32),
        ):
            with self.subTest(ack=ack):
                with mock.patch.object(ds.threading, "Thread", InlineThread), \
                        mock.patch.object(ds, "urlopen", return_value=JsonReply(ack)):
                    self.runner.accept(self.response(), self.clock())
                self.assertEqual([], self.engine.created)
                self.assertNotIn(self.value["id"], self.runner.fence_acks)

    def test_late_fence_ack_cannot_revive_expired_grant(self):
        reply = JsonReply(self.acknowledgement(), before_read=lambda: self.clock.advance(11))
        with mock.patch.object(ds.threading, "Thread", InlineThread), \
                mock.patch.object(ds, "urlopen", return_value=reply):
            self.runner.accept(self.response(), self.clock())
        self.assertEqual([], self.engine.created)
        self.assertNotIn(self.value["id"], self.runner.fence_acks)
        self.assertEqual({}, self.runner.grants)

    def test_old_fence_job_cannot_approve_a_replacement_grant(self):
        old = self.grant()
        new = self.grant(grant_token="d" * 32)
        new["deadline"] = self.clock() + 10
        self.runner.grants[self.value["id"]] = new
        with mock.patch.object(ds, "urlopen", return_value=JsonReply(self.acknowledgement())):
            self.runner._fence(self.value["id"], old)
        self.assertEqual({}, self.runner.fence_acks)
        self.assertEqual([], self.engine.created)


class RecordingEngine(ds.DockerEngine):
    def __init__(self):
        super().__init__("/unused-test-socket")
        self.calls = []
        self.exists = False
        self.running = False
        self.fail_start = False
        self.labels = {ds.MANAGED: "true", ds.WORKER: WORKER_ID}

    def request(self, method, path, value=None, **unused):
        self.calls.append((method, path, copy.deepcopy(value)))
        if method == "POST" and path.startswith("/v1.24/containers/create?"):
            self.exists = True
            self.labels = value["Labels"]
            return {"Id": "created-container"}
        if method == "GET" and path.endswith("/json"):
            if not self.exists:
                error = ds.DockerError("not found")
                error.status = 404
                raise error
            return {"Id": "created-container", "Config": {"Labels": dict(self.labels)},
                    "State": {"Running": self.running}}
        if method == "POST" and path.endswith("/start"):
            if self.fail_start:
                raise ds.DockerError("engine rejected start")
            self.running = True
            return {}
        if method == "POST" and "/kill?" in path:
            self.running = False
            return {}
        if method == "DELETE":
            self.exists = False
            return {}
        raise AssertionError("Unexpected Docker API request: " + method + " " + path)


class DockerScopeTests(unittest.TestCase):
    def test_remove_refuses_both_unmanaged_and_other_workers_container(self):
        for labels in (
            {ds.MANAGED: "false", ds.WORKER: WORKER_ID},
            {ds.MANAGED: "true", ds.WORKER: "different-worker"},
        ):
            with self.subTest(labels=labels):
                engine = RecordingEngine()
                engine.exists = engine.running = True
                engine.labels = labels
                with self.assertRaises(ds.DockerError):
                    engine.remove_managed("created-container", WORKER_ID)
                self.assertTrue(engine.exists)
                self.assertFalse(any(method in ("POST", "DELETE") for method, _, _ in engine.calls))

    def test_failed_start_cleans_its_created_container(self):
        engine = RecordingEngine()
        engine.fail_start = True
        grant = {"grant_token": GRANT_TOKEN, "boot_epoch": BOOT_EPOCH}
        with self.assertRaises(ds.DockerError):
            engine.create(manifest(), IMAGE, WORKER_ID, INCARNATION, grant)
        self.assertFalse(engine.exists, "Failed start left an orphan that blocks subsequent attempts")
        self.assertTrue(any(method == "DELETE" for method, _, _ in engine.calls))

    def test_daemon_restart_cannot_restart_children_without_runner_grant(self):
        engine = RecordingEngine()
        engine.create(manifest(), IMAGE, WORKER_ID, INCARNATION,
                      {"grant_token": GRANT_TOKEN, "boot_epoch": BOOT_EPOCH})
        created = next(value for method, path, value in engine.calls
                       if method == "POST" and "/containers/create?" in path)
        self.assertEqual("no", created["HostConfig"]["RestartPolicy"]["Name"])
        self.assertEqual(WORKER_ID, created["Labels"][ds.WORKER])
        self.assertEqual("true", created["Labels"][ds.MANAGED])


if __name__ == "__main__":
    unittest.main()
