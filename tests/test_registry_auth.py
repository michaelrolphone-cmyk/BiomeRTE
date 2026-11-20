import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from biomerte import docker_services as ds

IMAGE = "ghcr.io/owner/service@sha256:" + "a"*64
SECRET = "private-registry-test-secret"

class RegistryAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.write({"auths": {"ghcr.io": {"auth": base64.b64encode(
            ("owner:"+SECRET).encode()).decode()}}})
        self.engine = ds.DockerEngine(registry_auth_file=str(self.path))

    def write(self, value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def decode(self, header):
        return json.loads(base64.urlsafe_b64decode(header).decode())

    def test_exact_registry_and_docker_auth_decode(self):
        self.assertEqual(self.decode(self.engine._registry_auth_header(IMAGE)),
            {"username": "owner", "password": SECRET, "serveraddress": "ghcr.io"})
        for ref in ["ghcr.io.evil/owner/service@sha256:"+"a"*64,
                    "ghcr.io:443/owner/service@sha256:"+"a"*64,
                    "other.example/service@sha256:"+"a"*64,
                    "library/alpine@sha256:"+"a"*64]:
            self.assertIsNone(self.engine._registry_auth_header(ref))


    def test_unqualified_names_tags_and_digests_resolve_to_docker_hub(self):
        references = ["ghcr.io", "alpine:3", "localhost:5000", "ghcr.io@sha256:"+"a"*64]
        for reference in references:
            self.assertIsNone(self.engine._registry_auth_header(reference))
        self.write({"auths": {"docker.io": {"username": "hub", "password": "hub-secret"},
                             "example": {"username": "explicit", "password": "host-secret"}}})
        for reference in references:
            self.assertEqual(self.decode(self.engine._registry_auth_header(reference))["serveraddress"], "docker.io")
        self.assertEqual(self.decode(self.engine._registry_auth_header("Example/service:tag"))["serveraddress"], "example")

    def test_registry_keys_reject_trailing_newline(self):
        self.write({"auths": {"ghcr.io\n": {"username": "owner", "password": SECRET}}})
        with self.assertRaisesRegex(ds.DockerError, "unavailable or invalid"):
            self.engine._registry_auth_header(IMAGE)

    def test_atomic_file_rotation_is_used_on_next_pull(self):
        old = self.engine._registry_auth_header(IMAGE)
        replacement = self.path.with_name("replacement.json")
        replacement.write_text(json.dumps({"auths": {"ghcr.io": {"username": "owner", "password": "rotated"}}}))
        replacement.chmod(0o600)
        os.replace(str(replacement), str(self.path))
        self.assertEqual(self.decode(self.engine._registry_auth_header(IMAGE))["password"], "rotated")
        self.assertNotEqual(old, self.engine._registry_auth_header(IMAGE))

    def test_missing_symlink_nonregular_and_open_permissions_fail_closed(self):
        for mode in (0o644, 0o601, 0o700):
            self.path.chmod(mode)
            with self.assertRaisesRegex(ds.DockerError, "unavailable or invalid"):
                self.engine._registry_auth_header(IMAGE)
        self.path.chmod(0o600)
        target = self.path.with_name("actual.json")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ds.DockerError, "unavailable or invalid"):
            self.engine._registry_auth_header(IMAGE)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaisesRegex(ds.DockerError, "unavailable or invalid"):
            self.engine._registry_auth_header(IMAGE)
        self.path.rmdir()
        with self.assertRaisesRegex(ds.DockerError, "unavailable or invalid"):
            self.engine._registry_auth_header(IMAGE)
        if hasattr(os, "mkfifo"):
            os.mkfifo(str(self.path), 0o600)
            with self.assertRaisesRegex(ds.DockerError, "unavailable or invalid"):
                self.engine._registry_auth_header(IMAGE)

    def test_helpers_malformed_and_ambiguous_credentials_are_not_used(self):
        values = [
            {"auths": {}, "credsStore": "osxkeychain"},
            {"auths": {"ghcr.io": {"auth": SECRET}}},
            {"auths": {"https://ghcr.io": {"username": "owner", "password": SECRET}}},
            {"auths": {"ghcr.io": {"username": "owner", "password": SECRET, "auth": "anything"}}},
            {"auths": {"ghcr.io": {"username": "", "password": SECRET}}},
            {"auths": {"ghcr.io": {"username": "owner", "password": SECRET+"\n"}}},
        ]
        for value in values:
            self.write(value)
            with self.assertRaises(ds.DockerError) as raised:
                self.engine._registry_auth_header(IMAGE)
            self.assertNotIn(SECRET, str(raised.exception))

    def test_header_is_only_sent_to_image_create(self):
        reply = mock.Mock(status=200)
        reply.readline.side_effect = [b'{"status":"ready"}\n', b""]
        connection = mock.Mock()
        connection.getresponse.return_value = reply
        with mock.patch.object(ds, "UnixConnection", return_value=connection), \
                mock.patch.object(self.engine, "image", return_value={"Id": "prepared"}):
            self.engine.pull(IMAGE)
        method, path = connection.request.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertTrue(path.startswith("/v1.24/images/create?"))
        header = connection.request.call_args.kwargs["headers"]["X-Registry-Auth"]
        self.assertEqual(self.decode(header)["password"], SECRET)
        with self.assertRaisesRegex(ds.DockerError, "only valid for image pulls"):
            self.engine.request("GET", "/version", registry_auth=header)

    def test_unrelated_registry_and_no_config_keep_anonymous_request(self):
        other = "other.example/service@sha256:"+"a"*64
        for engine, ref in [(self.engine, other), (ds.DockerEngine(), IMAGE)]:
            with mock.patch.object(engine, "request") as request, \
                    mock.patch.object(engine, "image", return_value={"Id": "prepared"}):
                engine.pull(ref)
            self.assertEqual(request.call_args.kwargs, {"timeout": None, "stream": True})

    def test_authenticated_pull_errors_cannot_leak_registry_echo(self):
        with mock.patch.object(self.engine, "request", side_effect=ds.DockerError("echo "+SECRET)):
            with self.assertRaises(ds.DockerError) as raised:
                self.engine.pull(IMAGE)
        self.assertEqual(str(raised.exception), "Authenticated registry image pull failed")
        self.assertNotIn(SECRET, str(raised.exception))

    def test_invalid_auth_file_never_reaches_docker(self):
        self.path.unlink()
        with mock.patch.object(self.engine, "request") as request:
            with self.assertRaises(ds.DockerError):
                self.engine.pull(IMAGE)
            request.assert_not_called()

    def test_relative_or_inline_configuration_is_rejected(self):
        for value in ["relative/config.json", "", {"password": SECRET}]:
            with self.assertRaises(ValueError):
                ds.DockerEngine(registry_auth_file=value)

    def test_local_image_ids_never_read_registry_credentials(self):
        with mock.patch.object(self.engine, "_registry_auth_header") as credentials:
            with self.assertRaisesRegex(ds.DockerError, "already exist"):
                self.engine.pull("sha256:"+"a"*64)
            credentials.assert_not_called()
