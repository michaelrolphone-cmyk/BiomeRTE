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

if __name__ == "__main__":
    unittest.main()
