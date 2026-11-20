"""Exercise Docker provider placement through the real common gateway adapter."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MacDockerDispatch(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(sys, "path", sys.path[:]):
            self.gateway = load_module("docker_gateway_fixture", ROOT / "providers/mac-terminal/mac_provider.py")
            with mock.patch.dict(sys.modules, {"mac_provider": self.gateway}):
                self.provider = load_module("docker_provider_fixture", ROOT / "providers/mac-docker/mac_docker.py")
        self.cfg = {
            "provider": "a" * 64,
            "site": "existing-site",
            "host": "existing-native-mac.local",
            "controller": "retired-native-controller",
        }

    def test_stale_controller_pin_does_not_override_provider_placement(self):
        remote = self.provider.Remote(self.cfg)
        result = {"accepted": True}
        with mock.patch.dict(self.gateway.os.environ, {"BIOME_PROVIDER_HOST": self.cfg["host"]}), \
                mock.patch.object(self.gateway.socket, "gethostname", return_value="linux-container"), \
                mock.patch.object(self.gateway, "invoke", return_value=result) as invoke:
            for operation in ("cap.offer", "provider.poll", "cap.progress", "cap.result"):
                with self.subTest(operation=operation):
                    arguments = {"provider": self.cfg["provider"], "nested": {"operation": "existing-work"}}
                    rid = "existing-request-" + operation
                    self.assertIs(remote.call(operation, arguments, rid), result)
                    invoke.assert_called_with(operation, arguments, rid, "site",
                        provider_context={"provider": self.cfg["provider"], "host": self.cfg["host"]})
                    self.assertIs(invoke.call_args.args[1], arguments)
        self.assertEqual(remote.node, self.cfg["provider"])
        self.assertEqual(remote.site, self.cfg["site"])
        self.assertEqual(self.cfg["controller"], "retired-native-controller")

    def test_absent_request_id_and_explicit_realm_are_preserved(self):
        cfg = dict(self.cfg);cfg.pop("controller")
        remote = self.provider.Remote(cfg);remote.realm = "local"
        arguments = {"provider": cfg["provider"]}
        with mock.patch.dict(self.gateway.os.environ, {"BIOME_PROVIDER_HOST": cfg["host"]}), \
                mock.patch.object(self.gateway, "invoke", return_value={"ok": True}) as invoke:
            remote.call("provider.poll", arguments)
        invoke.assert_called_once_with("provider.poll", arguments, None, "local",
            provider_context={"provider": cfg["provider"], "host": cfg["host"]})

    def test_dispatch_error_is_propagated_without_caller_replay(self):
        remote = self.provider.Remote(self.cfg)
        with mock.patch.dict(self.gateway.os.environ, {"BIOME_PROVIDER_HOST": self.cfg["host"]}), \
                mock.patch.object(self.gateway, "invoke", side_effect=RuntimeError("reply unavailable")) as invoke:
            with self.assertRaisesRegex(RuntimeError, "reply unavailable"):
                remote.call("cap.result", {"operation": "existing-work"}, "stable-request")
        self.assertEqual(invoke.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
