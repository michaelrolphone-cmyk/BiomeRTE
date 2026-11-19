"""Failure-boundary tests for service state preparation and activation."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from biomerte import docker_services as ds
from biomerte import service_adapters as sa
from tests.test_docker_services import (
    BOOT_EPOCH, GRANT_TOKEN, IMAGE, INCARNATION, WORKER_ID,
    Clock, FakeCoordinator, FakeDocker, InlineThread, JsonReply,
    RecordingEngine, RunnerCase, manifest,
)

REAL_THREAD = threading.Thread
CHECKPOINT = "c" * 64


def staged_manifest(**changes):
    value = dict(
        effect_policy="externally_fenced",
        state_policy="externally_managed",
        fence_hook={"url": "https://sink.invalid/fence"},
        activation={"protocol": sa.PROTOCOL, "binding": "authority"},
        mounts=[
            {"binding": "private", "target": "/config", "role": "private", "read_only": True},
            {"binding": "state", "target": "/state", "role": "state", "read_only": False},
        ],
    )
    value.update(changes)
    return manifest(**value)


def host_config(service_id="test-service"):
    return {
        "bindings": {
            "private": {"source": "/host/private", "role": "private", "service_id": service_id},
            "state": {"source": "/host/state", "role": "state", "service_id": service_id},
        },
        "adapters": {
            "authority": {"url": "https://adapter.invalid/control", "service_id": service_id}
        },
    }


def prepare_reply(body, **state):
    value = dict(checkpoint=CHECKPOINT, fresh=True, custody_complete=True, ready=True)
    value.update(state)
    return dict(protocol=sa.PROTOCOL, id=body["id"], binding=body["binding"],
                phase="prepared", state=value)


def activate_reply(body):
    return dict(protocol=sa.PROTOCOL, id=body["id"], binding=body["binding"],
                phase="active", checkpoint=body["checkpoint"])


class StagingDocker(FakeDocker):
    def create(self, value, image_ref, worker, incarnation, grant, overrides=None, **extra):
        result = super().create(value, image_ref, worker, incarnation, grant, overrides, **extra)
        self.created[-1]["extra"] = copy.deepcopy({k: v for k, v in extra.items()
                                                 if k not in ("admit", "on_created")})
        return result


class StagedCase(RunnerCase):
    def setUp(self):
        super().setUp()
        self.value = staged_manifest()
        self.write_manifest(self.value)
        self.engine = StagingDocker()
        self.runner.engine = self.engine
        self.runner.config.update(host_config())
        self.adapter_calls = []
        self.adapter_reply = None
        self.fence_reply = None
        self.prepare()

    def grant(self, **changes):
        value = super().grant(state="awaiting_external_fence", effect_ready=False, state_ready=True)
        value.update(changes)
        return value

    def adapter(self, body):
        self.adapter_calls.append(copy.deepcopy(body))
        if self.adapter_reply:
            return self.adapter_reply(body)
        return prepare_reply(body) if body["operation"] == "prepare" else activate_reply(body)

    def fence(self, request, **unused):
        value = json.loads(request.data.decode())
        bound = {k: value[k] for k in sa.BINDING_FIELDS}
        result = dict(fenced=True, boot_epoch=value["boot_epoch"],
                      grant_token=value["grant_token"], binding=bound,
                      checkpoint=value["prepared_state"]["checkpoint"])
        if self.fence_reply:
            result = self.fence_reply(result)
        return JsonReply(result)

    def step(self, grant=None):
        with mock.patch.object(ds.threading, "Thread", InlineThread), \
                mock.patch.object(sa.AdapterClient, "call", side_effect=self.adapter), \
                mock.patch.object(ds, "urlopen", side_effect=self.fence):
            self.runner.accept(self.response(grant or self.grant()), self.clock())

    def fenced(self):
        self.step()  # Verify scoped state before creating any child.
        self.step()  # Create an inert child and obtain the bound sink proof.
        return self.grant(state="starting", effect_ready=True)

    def active(self):
        admitted = self.fenced()
        self.step(admitted)
        self.step(admitted)
        return admitted


class PreparationTests(StagedCase):
    def test_state_proof_precedes_inert_child_and_normal_health_waits_activation(self):
        before = self.runner.advertisements()[0]
        self.assertFalse(before["state_ready"])
        self.step()
        self.assertEqual([], self.engine.created)
        report = self.runner.advertisements()[0]
        self.assertTrue(report["state_ready"])
        self.assertEqual(sa.binding(self.grant()), report["state_proof"]["binding"])
        self.assertEqual(CHECKPOINT, report["state_proof"]["checkpoint"])
        self.step()
        self.assertEqual(1, len(self.engine.created))
        self.assertEqual("preparing", self.engine.created[0]["extra"]["initial_phase"])
        self.assertEqual(CHECKPOINT, self.engine.created[0]["extra"]["checkpoint"])
        self.assertIsNone(self.runner.advertisements()[0]["healthy"])
        self.assertEqual(["prepare"], [v["operation"] for v in self.adapter_calls])
        admitted = self.grant(state="starting", effect_ready=True)
        self.step(admitted)
        self.assertTrue(self.runner.advertisements()[0]["healthy"])
        self.step(admitted)
        self.assertEqual(1, len(self.engine.created))
        self.assertEqual(["prepare", "activate"], [v["operation"] for v in self.adapter_calls])

    def test_prepared_state_requires_explicit_coordinator_ack_before_child_or_fence(self):
        self.step(self.grant(state_ready=False))
        self.assertTrue(self.runner.advertisements()[0]["state_ready"])
        for unacknowledged in (False, None, 1, "true"):
            self.step(self.grant(state_ready=unacknowledged))
            self.assertEqual([], self.engine.created)
            self.assertEqual({}, self.runner.fence_acks)
        self.step(self.grant(state_ready=True))
        self.assertEqual(1, len(self.engine.created))
        self.assertEqual(CHECKPOINT, self.runner.fence_acks[self.value["id"]]["checkpoint"])

    def test_prepare_finishing_during_heartbeat_waits_for_next_acknowledged_report(self):
        reported = []
        def heartbeat(operation, body):
            reported.append(copy.deepcopy(body["services"][0]))
            if len(reported) == 1:
                request = sa.request("prepare", self.grant())
                self.runner.state_proofs[self.value["id"]] = sa.prepared(
                    request, prepare_reply(request))
                return self.response(self.grant(state_ready=False))
            self.assertTrue(body["services"][0]["state_ready"])
            return self.response(self.grant(state_ready=True))
        with mock.patch.object(self.client, "call", side_effect=heartbeat), \
                mock.patch.object(ds, "urlopen", side_effect=self.fence):
            self.runner.tick()
            self.assertEqual([], self.engine.created)
            self.assertEqual({}, self.runner.fence_acks)
            self.runner.tick()
        self.assertFalse(reported[0]["state_ready"])
        self.assertTrue(reported[1]["state_ready"])
        self.assertEqual(1, len(self.engine.created))
        self.assertEqual(CHECKPOINT, self.runner.fence_acks[self.value["id"]]["checkpoint"])

    def test_unavailable_stale_or_incomplete_custody_never_creates_child(self):
        for fields in (
            {"ready": False}, {"fresh": False}, {"custody_complete": False},
            {"checkpoint": "unknown"}, {"fresh": 1}, {"ready": "true"},
        ):
            with self.subTest(fields=fields):
                self.adapter_reply = lambda body: prepare_reply(body, **fields)
                self.step()
                self.assertEqual({}, self.runner.state_proofs)
                self.assertEqual([], self.engine.created)
                self.assertFalse(self.runner.advertisements()[0]["state_ready"])

    def test_unavailable_adapter_does_not_admit_state(self):
        self.adapter_reply = mock.Mock(side_effect=OSError("offline"))
        self.step()
        self.assertEqual({}, self.runner.state_proofs)
        self.assertEqual([], self.engine.created)

    def test_lost_prepare_reply_retries_same_id_without_starting_a_child(self):
        seen = []
        def lost(body):
            seen.append(body["id"])
            if len(seen) == 1:
                raise TimeoutError("reply lost")
            return prepare_reply(body)
        self.adapter_reply = lost
        self.step()
        self.assertEqual([], self.engine.created)
        self.step()
        self.assertEqual([seen[0], seen[0]], seen)
        self.assertEqual([], self.engine.created)
        self.step()
        self.assertEqual(1, len(self.engine.created))

    def test_wrong_prepare_binding_id_or_protocol_is_rejected(self):
        for field, value in (("binding", {}), ("id", "wrong"), ("protocol", "wrong")):
            with self.subTest(field=field):
                def wrong(body):
                    answer = prepare_reply(body)
                    answer[field] = value
                    return answer
                self.adapter_reply = wrong
                self.step()
                self.assertEqual({}, self.runner.state_proofs)
                self.assertEqual([], self.engine.created)

    def test_changed_grant_boot_or_manifest_discards_inflight_prepare_reply(self):
        for field, value in (
            ("grant_token", "next-grant"), ("boot_epoch", "next-boot"),
            ("manifest_revision", "d" * 64), ("worker_incarnation", "another-incarnation"),
        ):
            with self.subTest(field=field):
                original = self.grant()
                def change_during_reply(body):
                    replacement = dict(original, deadline=self.clock() + 10)
                    replacement[field] = value
                    self.runner.grants[self.value["id"]] = replacement
                    return prepare_reply(body)
                self.adapter_reply = change_during_reply
                self.step(original)
                self.assertEqual({}, self.runner.state_proofs)
                self.assertEqual([], self.engine.created)

    def test_expiry_during_prepare_cannot_cache_a_ready_checkpoint(self):
        def expired(body):
            self.clock.advance(11)
            self.runner.expire()
            return prepare_reply(body)
        self.adapter_reply = expired
        self.step()
        self.assertEqual({}, self.runner.grants)
        self.assertEqual({}, self.runner.state_proofs)
        self.assertEqual([], self.engine.created)

    def test_watchdog_expires_while_prepare_network_call_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(body):
            entered.set()
            release.wait(3)
            return prepare_reply(body)
        with mock.patch.object(sa.AdapterClient, "call", side_effect=blocked), \
                mock.patch.object(ds.threading, "Thread", REAL_THREAD):
            self.runner.accept(self.response(), self.clock())
            self.assertTrue(entered.wait(2))
            watchdog = threading.Thread(target=self.runner.watchdog)
            watchdog.start()
            try:
                self.clock.advance(11)
                until = time.monotonic() + 2
                while self.runner.grants and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual({}, self.runner.grants)
                self.assertEqual([], self.engine.created)
            finally:
                self.runner.stop_event.set()
                watchdog.join(2)
                jobs = list(self.runner.adapter_jobs.values())
                release.set()
                for job in jobs:
                    job.join(2)
        self.assertEqual({}, self.runner.state_proofs)

    def test_host_binding_mismatch_blocks_preparation(self):
        variants = (
            ("state", "service_id", "another-service"),
            ("state", "role", "cache"),
            ("state", "source", "relative-path"),
            ("state", "read_only", True),
        )
        for name, field, wrong in variants:
            with self.subTest(field=field):
                self.runner.config.update(host_config())
                self.runner.config["bindings"][name][field] = wrong
                report = self.runner.advertisements()[0]
                self.assertFalse(report["dependencies_ready"])
                self.step()
                self.assertEqual([], self.adapter_calls)
                self.assertEqual([], self.engine.created)

    def test_host_adapter_identity_mismatch_blocks_preparation(self):
        self.runner.config["adapters"]["authority"]["service_id"] = "another-service"
        self.assertFalse(self.runner.advertisements()[0]["dependencies_ready"])
        self.step()
        self.assertEqual([], self.adapter_calls)
        self.assertEqual([], self.engine.created)


class ActivationTests(StagedCase):
    def test_fence_proof_must_match_full_binding_and_prepared_checkpoint(self):
        self.step()
        for field, value in (("binding", {}), ("checkpoint", "d" * 64)):
            with self.subTest(field=field):
                self.fence_reply = lambda proof: dict(proof, **{field: value})
                self.step(self.grant(state="starting", effect_ready=True))
                self.assertEqual({}, self.runner.fence_acks)
                self.assertEqual(["prepare"], [v["operation"] for v in self.adapter_calls])
                self.assertIsNone(self.runner.advertisements()[0]["healthy"])

    def test_sink_fence_alone_cannot_activate_without_coordinator_admission(self):
        self.fenced()
        for state, ready in (("starting", False), ("active", 1),
                             ("awaiting_external_fence", True)):
            self.step(self.grant(state=state, effect_ready=ready))
        self.assertEqual(["prepare"], [v["operation"] for v in self.adapter_calls])
        self.assertEqual({}, self.runner.activation_acks)

    def test_lost_activation_reply_reuses_id_and_does_not_restart_or_repeat_activation(self):
        admitted = self.fenced()
        committed, ids = set(), []
        def lost(body):
            ids.append(body["id"])
            if body["id"] not in committed:
                committed.add(body["id"])
                raise TimeoutError("activated, reply lost")
            return activate_reply(body)
        self.adapter_reply = lost
        self.step(admitted)
        self.assertEqual({}, self.runner.activation_acks)
        self.assertIsNone(self.runner.advertisements()[0]["healthy"])
        self.step(admitted)
        self.assertEqual([ids[0], ids[0]], ids)
        self.assertEqual(1, len(committed))
        self.assertEqual(1, len(self.engine.created))
        self.assertTrue(self.runner.advertisements()[0]["healthy"])

    def test_changed_binding_discards_activation_reply(self):
        admitted = self.fenced()
        def change(body):
            self.runner.grants[self.value["id"]] = dict(
                admitted, grant_token="replacement", deadline=self.clock() + 10)
            return activate_reply(body)
        self.adapter_reply = change
        self.step(admitted)
        self.assertEqual({}, self.runner.activation_acks)
        self.assertIsNone(self.runner.advertisements()[0]["healthy"])

    def test_bad_activation_checkpoint_never_reports_active(self):
        admitted = self.fenced()
        self.adapter_reply = lambda body: dict(activate_reply(body), checkpoint="d" * 64)
        self.step(admitted)
        self.assertEqual({}, self.runner.activation_acks)
        self.assertIsNone(self.runner.advertisements()[0]["healthy"])

    def test_expiry_during_activation_removes_staging_child(self):
        admitted = self.fenced()
        child = self.runner.containers[self.value["id"]]
        def expired(body):
            self.clock.advance(11)
            self.runner.expire()
            return activate_reply(body)
        self.adapter_reply = expired
        self.step(admitted)
        self.assertIn(child, self.engine.removed)
        self.assertEqual({}, self.runner.activation_acks)
        self.assertEqual({}, self.runner.state_proofs)
        self.assertEqual({}, self.runner.grants)

    def test_manifest_rollout_removes_old_stage_and_clears_all_admission_proofs(self):
        self.fenced()
        child = self.runner.containers[self.value["id"]]
        updated = staged_manifest(command=["updated-service"])
        self.runner.adopt_manifests({"manifests": [updated],
                                    "configured_revisions": {updated["id"]: updated["revision"]}})
        self.runner.accept({"boot_epoch": BOOT_EPOCH, "assignments": []}, self.clock())
        self.assertIn(child, self.engine.removed)
        self.assertEqual({}, self.runner.state_proofs)
        self.assertEqual({}, self.runner.activation_acks)
        self.assertEqual({}, self.runner.fence_acks)

    def test_changed_boot_stops_active_child_and_requires_new_state_fence_and_activation(self):
        self.active()
        child = self.runner.containers[self.value["id"]]
        replacement = self.grant(boot_epoch="next-boot")
        with mock.patch.object(ds.threading, "Thread", InlineThread), \
                mock.patch.object(sa.AdapterClient, "call", side_effect=self.adapter):
            self.runner.accept(self.response(replacement, boot_epoch="next-boot"), self.clock())
        self.assertIn(child, self.engine.removed)
        self.assertEqual({}, self.runner.activation_acks)
        self.assertEqual({}, self.runner.fence_acks)
        self.assertEqual("next-boot", self.runner.state_proofs[self.value["id"]]["binding"]["boot_epoch"])

    def test_staging_container_created_after_expiry_is_removed(self):
        self.step()
        self.engine.after_create = lambda: self.clock.advance(11)
        self.step()
        self.assertEqual(1, len(self.engine.created))
        self.assertIn(self.engine.created[0]["id"], self.engine.removed)
        self.assertEqual({}, self.runner.containers)
        self.assertEqual({}, self.runner.fence_acks)


class BindingAndWireTests(unittest.TestCase):
    def test_authoritative_state_cannot_use_cache_label_or_unstaged_policy(self):
        invalid = [
            staged_manifest(activation=None),
            staged_manifest(effect_policy="stateless"),
            staged_manifest(mounts=[{"binding": "state", "target": "/state", "role": "state",
                                     "read_only": False, "cache_only": True}]),
            staged_manifest(mounts=[{"binding": "private", "target": "/config", "role": "private",
                                     "read_only": False}]),
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ds.validate_manifest(value)

    def test_binding_resolves_per_host_without_changing_manifest_revision(self):
        value = staged_manifest()
        mac, pi = host_config(), host_config()
        pi["bindings"]["state"]["source"] = "/srv/service-state"
        before = value["revision"]
        self.assertEqual("/host/state", sa.resolve_mounts(value, mac["bindings"])[1]["source"])
        self.assertEqual("/srv/service-state", sa.resolve_mounts(value, pi["bindings"])[1]["source"])
        self.assertEqual(before, ds.revision(value))

    def test_docker_stage_has_explicit_inert_phase_checkpoint_and_scoped_labels(self):
        value = staged_manifest()
        grant = dict(service_id=value["id"], manifest_revision=value["revision"],
                     worker_id=WORKER_ID, worker_incarnation=INCARNATION,
                     boot_epoch=BOOT_EPOCH, grant_token=GRANT_TOKEN)
        engine = RecordingEngine()
        engine.create(value, IMAGE, WORKER_ID, INCARNATION, grant,
                      mount_bindings=host_config()["bindings"],
                      initial_phase="preparing", checkpoint=CHECKPOINT, adapter_url="https://adapter.invalid/control")
        create = next(call for call in engine.calls if call[0] == "POST" and "/containers/create" in call[1])
        config = create[2]
        self.assertIn("BIOME_SERVICE_PHASE=preparing", config["Env"])
        self.assertIn("BIOME_SERVICE_CHECKPOINT=" + CHECKPOINT, config["Env"])
        self.assertIn("BIOME_SERVICE_ADAPTER_URL=https://adapter.invalid/control", config["Env"])
        self.assertIn("/host/state:/state:rw", config["HostConfig"]["Binds"])
        self.assertIn("/host/private:/config:ro", config["HostConfig"]["Binds"])
        self.assertEqual({"Name": "no"}, config["HostConfig"]["RestartPolicy"])
        self.assertEqual(WORKER_ID, config["Labels"][ds.WORKER])
        self.assertEqual("true", config["Labels"][ds.MANAGED])

    def test_adapter_local_http_requires_explicit_private_endpoint_opt_in(self):
        value, config = staged_manifest(), host_config()
        local = config["adapters"]["authority"]
        local["url"] = "http://host.docker.internal:18740/internal/service"
        with self.assertRaises(ValueError):
            sa.AdapterClient(config, value)
        local["allow_local_http"] = True
        sa.AdapterClient(config, value)
        local["url"] = "http://example.com/control"
        with self.assertRaises(ValueError):
            sa.AdapterClient(config, value)
        local["url"] = "https://user:password@example.com/control"
        with self.assertRaises(ValueError):
            sa.AdapterClient(config, value)

    def test_wire_request_identity_is_stable_across_retry_but_changes_with_binding(self):
        grant = dict(service_id="test-service", manifest_revision="a" * 64,
                     worker_id="mac", worker_incarnation="incarnation",
                     boot_epoch="epoch", grant_token="token")
        one = sa.request("prepare", grant)
        self.assertEqual(one, sa.request("prepare", dict(grant)))
        self.assertNotEqual(one["id"], sa.request("activate", grant)["id"])
        for field in sa.BINDING_FIELDS:
            changed = dict(grant, **{field: "b" * 64})
            self.assertNotEqual(one["id"], sa.request("prepare", changed)["id"])


if __name__ == "__main__":
    unittest.main()
