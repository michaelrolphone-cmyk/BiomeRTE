import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("container_images",
    str(Path(__file__).resolve().parents[1] / "deploy/docker-services/images.py"))
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)

class ImageReleaseTests(unittest.TestCase):
    def test_release_rejects_armv7_runtime_labeled_arm64_and_reclaims_inspection(self):
        image = "sha256:" + "a" * 64
        arm32 = bytearray(20)
        arm32[:6] = b"\x7fELF\x01\x01"
        arm32[18:20] = (40).to_bytes(2, "little")

        def engine(argv):
            if argv[1:3] == ["image", "inspect"]:
                return json.dumps([{"Id": image, "Os": "linux", "Architecture": "arm64",
                                    "Config": {"Entrypoint": ["python"]}}])
            if argv[1] == "create":
                return "test-inspection-container"
            if argv[1] == "cp":
                self.assertEqual("test-inspection-container:/usr/local/bin/python", argv[-2])
                Path(argv[-1]).write_bytes(arm32)
                return ""
            self.assertEqual(["docker", "rm", "-v", "test-inspection-container"], argv)
            return ""

        with patch.object(images, "call", side_effect=engine) as invoke:
            with self.assertRaisesRegex(ValueError, "does not match linux/arm64"):
                images.inspect_image("docker", image, "linux/arm64")
            self.assertEqual("rm", invoke.call_args.args[0][1])
        images.validate_elf(arm32, "linux/arm/v7")
        arm32[4] = 2
        arm32[18:20] = (183).to_bytes(2, "little")
        images.validate_elf(arm32, "linux/arm64")

    def test_direct_build_checks_actual_python_against_requested_target(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "python"
            with patch.object(images.sys, "executable", str(binary)):
                header = bytearray(20)
                header[:6] = b"\x7fELF\x01\x01"
                header[18:20] = (40).to_bytes(2, "little")
                binary.write_bytes(header)
                self.assertEqual(images.runtime_platform(Namespace(platform="linux/arm/v7")),
                                 {"runtime_platform": "linux/arm/v7"})
                with self.assertRaisesRegex(ValueError, "does not match"):
                    images.runtime_platform(Namespace(platform="linux/arm64"))
                header[4] = 2
                header[18:20] = (183).to_bytes(2, "little")
                binary.write_bytes(header)
                self.assertEqual(images.runtime_platform(Namespace(platform="linux/arm64/v8")),
                                 {"runtime_platform": "linux/arm64"})

    def sample(self, directory):
        artifact = directory / "service-linux-arm64.tar"
        artifact.write_bytes(b"verified native export")
        lock = {"schema": "biomerte-container-release/1", "complete": True,
            "source_commit": "a" * 40, "source_url": "https://github.com/example/runtime.git", "images": [{
                "id": "service", "platform": "linux/arm64",
                "image_id": "sha256:" + "b" * 64,
                "archive": {"file": artifact.name, "sha256": images.checksum(artifact),
                            "bytes": artifact.stat().st_size}, "registry_ref": None}]}
        path = directory / "images.lock.json"
        images.save_json(path, lock)
        return path, lock, artifact

    def test_export_integrity_detects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            path, lock, artifact = self.sample(Path(folder))
            args = type("Args", (), {"lock": path})()
            self.assertTrue(images.verify(args)["verified"])
            artifact.write_bytes(b"changed native export")
            with self.assertRaisesRegex(ValueError, "integrity"):
                images.verify(args)

    def test_incomplete_build_cannot_be_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path, lock, artifact = self.sample(Path(folder))
            lock["complete"] = False
            images.save_json(path, lock)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                images.release(path)

    def test_duplicate_architecture_and_mutable_ids_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path, lock, artifact = self.sample(Path(folder))
            lock["images"].append(dict(lock["images"][0]))
            images.save_json(path, lock)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                images.release(path)
            lock["images"] = lock["images"][:1]
            lock["images"][0]["image_id"] = "service:latest"
            images.save_json(path, lock)
            with self.assertRaisesRegex(ValueError, "immutable"):
                images.release(path)

    def test_archive_cannot_escape_release_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path, lock, artifact = self.sample(Path(folder))
            lock["images"][0]["archive"]["file"] = "../private.tar"
            images.save_json(path, lock)
            with self.assertRaisesRegex(ValueError, "sibling"):
                images.release(path)

    def test_source_credentials_are_not_image_metadata(self):
        self.assertEqual(images.source_url("https://user:secret@github.com/org/repo.git?token=secret"),
                         "https://github.com/org/repo.git")
        self.assertEqual(images.source_url("git@github.com:org/repo.git"),
                         "https://github.com/org/repo.git")

    def test_publish_requires_a_concrete_credential_free_registry(self):
        for registry in ["", "registry", "https://registry.example", "user:secret@registry.example"]:
            with self.assertRaises(ValueError):
                images.registry_prefix(registry, "namespace")
        for namespace in ["", "../private", "a/../b"]:
            with self.assertRaises(ValueError):
                images.registry_prefix("registry.example", namespace)
        self.assertEqual(images.registry_prefix("registry.example:5000", "team/runtime"),
                         "registry.example:5000/team/runtime")

    def test_build_plan_cannot_escape_committed_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            root.mkdir()
            outside = Path(folder) / "external.json"
            outside.write_text("{}")
            (root / "linked.json").symlink_to(outside)
            for requested in [str(outside), "../external.json", "linked.json", ""]:
                with self.assertRaisesRegex(ValueError, "snapshot"):
                    images.plan_path(root, requested)
            self.assertEqual(images.plan_path(root, "deploy/images.json"),
                             (root / "deploy/images.json").resolve())

    def test_publish_validates_all_target_provenance_before_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            path, lock, artifact = self.sample(Path(folder))
            first = lock["images"][0]
            second = dict(first, id="other-service", image_id="sha256:" + "c" * 64)
            lock["images"].append(second)
            images.save_json(path, lock)
            args = type("Args", (), {"lock": path, "docker": "docker",
                "registry": "registry.example", "namespace": "team"})()
            for mismatched in ["org.opencontainers.image.revision",
                               "org.opencontainers.image.source",
                               "io.biomerte.image", "io.biomerte.platform"]:
                def inspect(docker, image, platform):
                    item = first if image == first["image_id"] else second
                    labels = images.expected_labels(lock["source_commit"],
                        lock["source_url"], item["id"], item["platform"])
                    if item is second:
                        labels[mismatched] = "different"
                    return {"Config": {"Labels": labels}}
                with patch.object(images, "inspect_image", side_effect=inspect):
                    with patch.object(images, "call") as mutation:
                        with self.assertRaisesRegex(ValueError, "provenance"):
                            images.publish(args)
                        mutation.assert_not_called()

    def test_base_digest_uses_requested_registry_platform_not_cached_tag(self):
        def manifest(arch, variant, letter):
            return {"Descriptor": {"platform": {"os": "linux", "architecture": arch,
                    "variant": variant}, "digest": "sha256:" + letter * 64}}
        payload = [manifest("arm", "v6", "a"), manifest("arm", "v7", "b"),
                   manifest("arm64", "v8", "c"), manifest("unknown", "", "d")]
        with patch.object(images, "call", return_value=json.dumps(payload)) as command:
            self.assertEqual(images.resolve_base("docker", "registry.example:5000/python:3.11",
                                                "linux/arm/v7"),
                             "registry.example:5000/python@sha256:" + "b" * 64)
            command.assert_called_once_with(["docker", "manifest", "inspect", "--verbose",
                                             "registry.example:5000/python:3.11"])
        with patch.object(images, "call", return_value=json.dumps(payload)):
            self.assertEqual(images.resolve_base("docker", "python:3.11-alpine", "linux/arm64"),
                             "python@sha256:" + "c" * 64)
            with self.assertRaisesRegex(ValueError, "one base manifest"):
                images.resolve_base("docker", "python:3.11-alpine", "linux/amd64")

    def test_engine_architecture_is_detected_without_host_names(self):
        for architecture, expected in [("aarch64", "linux/arm64"),
                                       ("x86_64", "linux/amd64"),
                                       ("armv7l", "linux/arm/v7")]:
            with patch.object(images, "call", return_value=json.dumps(
                    {"OSType": "linux", "Architecture": architecture})):
                self.assertEqual(images.engine_platform("docker"), expected)
        with patch.object(images, "call", return_value=json.dumps(
                {"OSType": "windows", "Architecture": "amd64"})):
            with self.assertRaises(ValueError):
                images.engine_platform("docker")

    def test_mutable_registry_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path, lock, artifact = self.sample(Path(folder))
            lock["images"][0]["registry_ref"] = "registry.example/service:latest"
            images.save_json(path, lock)
            with self.assertRaisesRegex(ValueError, "Registry reference"):
                images.release(path)

    def test_render_requires_preload_or_confirmed_registry_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            path, lock, artifact = self.sample(Path(folder))
            template = Path(folder) / "template.json"
            template.write_text(json.dumps({"id": "portable-test",
                "effect_policy": "stateless", "state_policy": "ephemeral", "revision": "old"}))
            args = type("Args", (), {"lock": path, "image": "service", "template": template,
                                    "output": Path(folder) / "manifest.json", "local": False})()
            with self.assertRaisesRegex(ValueError, "not published"):
                images.render(args)
            args.local = True
            result = images.render(args)
            manifest = images.read_json(args.output)
            from biomerte.docker_services import revision
            self.assertEqual(result["revision"], revision(manifest))
            self.assertEqual(manifest["images"]["linux/arm64"], "sha256:" + "b" * 64)
            self.assertEqual(manifest["effect_policy"], "stateless")

    def test_combine_preserves_two_platforms_and_extra_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = []
            for platform in ("linux/arm64", "linux/amd64"):
                source = root / platform.rsplit("/", 1)[-1]
                source.mkdir()
                path, lock, _ = self.sample(source)
                lock["images"][0]["platform"] = platform
                lock["images"][0]["secondary_source"] = {"commit": "c"*40}
                images.save_json(path, lock)
                paths.append(path)
            args = type("Args", (), {"lock": paths, "output_dir": root/"combined"})()
            result = images.combine(args)
            combined = images.release(result["lock"])
            self.assertEqual(len(combined["images"]), 2)
            self.assertEqual({x["platform"] for x in combined["images"]}, {"linux/arm64", "linux/amd64"})
            self.assertTrue(all(x["secondary_source"]["commit"] == "c"*40 for x in combined["images"]))
            self.assertTrue(images.verify(type("Args", (), {"lock": result["lock"]})())["verified"])

    def test_combine_rejects_different_sources_duplicates_and_corruption_before_output(self):
        for change in ("source", "duplicate", "archive", "extra"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                paths = []
                for name in ("first", "second"):
                    source = root/name
                    source.mkdir()
                    path, lock, artifact = self.sample(source)
                    if name == "second":
                        lock["images"][0]["platform"] = "linux/amd64"
                        if change == "source":
                            lock["source_commit"] = "d"*40
                        elif change == "duplicate":
                            lock["images"][0]["platform"] = "linux/arm64"
                        elif change == "archive":
                            artifact.write_bytes(b"corrupt")
                        elif change == "extra":
                            lock["other_provenance"] = "different"
                        images.save_json(path, lock)
                    paths.append(path)
                output = root/"combined"
                with self.assertRaises(ValueError):
                    images.combine(type("Args", (), {"lock": paths, "output_dir": output})())
                self.assertFalse(output.exists())

    def test_repository_prefix_changes_only_published_repository_not_logical_provenance(self):
        for prefix, repository in [(None, "service"), ("biomerte-", "biomerte-service")]:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as folder:
                path, lock, _ = self.sample(Path(folder))
                args = type("Args", (), {"lock": path, "docker": "docker",
                    "registry": "ghcr.io", "namespace": "owner"})()
                if prefix is not None:
                    args.repository_prefix = prefix
                labels = images.expected_labels(lock["source_commit"], lock["source_url"],
                                                "service", "linux/arm64")
                with patch.object(images, "inspect_image", return_value={"Config": {"Labels": labels}}), \
                        patch.object(images, "call", return_value="digest: sha256:"+"c"*64) as command:
                    images.publish(args)
                expected_tag = "ghcr.io/owner/{}:{}-linux-arm64".format(repository, "a"*12)
                self.assertEqual(command.call_args_list[0].args[0],
                    ["docker", "tag", "sha256:"+"b"*64, expected_tag])
                self.assertEqual(command.call_args_list[1].args[0], ["docker", "push", expected_tag])
                published = images.release(path)["images"][0]
                self.assertEqual(published["id"], "service")
                self.assertEqual(published["image_id"], "sha256:"+"b"*64)
                self.assertEqual(published["registry_ref"], "ghcr.io/owner/"+repository+"@sha256:"+"c"*64)

    def test_invalid_repository_prefix_fails_before_image_inspection_or_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            path, _, _ = self.sample(Path(folder))
            for prefix in ("../", "bad/name", "Upper-", "registry:", "name@", " ", "-bad", None):
                args = type("Args", (), {"lock": path, "docker": "docker",
                    "registry": "ghcr.io", "namespace": "owner", "repository_prefix": prefix})()
                with self.subTest(prefix=prefix), patch.object(images, "inspect_image") as inspect, \
                        patch.object(images, "call") as mutation:
                    with self.assertRaisesRegex(ValueError, "Repository prefix"):
                        images.publish(args)
                    inspect.assert_not_called()
                    mutation.assert_not_called()

if __name__ == "__main__":
    unittest.main()
