"""Replica wiring checks without SSH, backup transfers or native media changes."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]

def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class ReplicaContainerWiring(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.env=mock.patch.dict(os.environ,{"BIOME_STORAGE_HOME":str(self.base)},clear=True)
        self.env.start();self.addCleanup(self.env.stop)
        with mock.patch.object(sys,"path",sys.path[:]):
            sys.path.insert(0,str(ROOT))
            volumes=load_module("replica_volumes_fixture",ROOT/"providers/mac-storage/volumes.py")
            with mock.patch.dict(sys.modules,{"volumes":volumes}):
                self.backup=load_module("replica_backup_fixture",ROOT/"providers/mac-storage/backup.py")
        self.config={"volume":"existing-volume","hosts":["192.168.1.46","old-name.local"]}
        self.backup.CONFIG.write_text(json.dumps(self.config))
        self.original=self.backup.CONFIG.read_bytes()
        self.ssh=mock.patch.object(self.backup.subprocess,"Popen",side_effect=AssertionError("No SSH in fixture"))
        self.ssh_mock=self.ssh.start();self.addCleanup(self.ssh.stop)
    def test_native_configuration_and_behavior_are_preserved_without_override(self):
        with mock.patch.object(self.backup,"prepare") as attest:
            self.assertEqual(self.backup.replica_preflight(),self.config)
        attest.assert_not_called();self.ssh_mock.assert_not_called()
        self.assertEqual(self.backup.CONFIG.read_bytes(),self.original)
    def test_current_address_override_preserves_volume_and_installed_config(self):
        with mock.patch.dict(os.environ,{"BIOME_REPLICA_HOST":"192.168.1.23"}):
            cfg=self.backup.replica_configuration()
        self.assertEqual(cfg,{"volume":"existing-volume","hosts":["192.168.1.23"]})
        self.assertEqual(self.backup.CONFIG.read_bytes(),self.original)
        self.ssh_mock.assert_not_called()
    def test_invalid_override_fails_before_storage_attestation_or_state_changes(self):
        with mock.patch.dict(os.environ,{"BIOME_REPLICA_HOST":"192.168.1.23;command","BIOME_STORAGE_PREMOUNTED":"1"}), \
                mock.patch.object(self.backup,"prepare") as attest:
            with self.assertRaises(ValueError):self.backup.sync()
        attest.assert_not_called();self.ssh_mock.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),["replica.json"])
    def test_container_requires_native_attestation_before_sync_effects(self):
        with mock.patch.dict(os.environ,{"BIOME_STORAGE_PREMOUNTED":"1","BIOME_REPLICA_HOST":"192.168.1.23"}), \
                mock.patch.object(self.backup,"prepare",side_effect=RuntimeError("native volume unavailable")) as attest:
            with self.assertRaisesRegex(RuntimeError,"native volume unavailable"):self.backup.sync()
        attest.assert_called_once_with();self.ssh_mock.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),["replica.json"])
    def test_readonly_preflight_returns_only_after_attestation(self):
        with mock.patch.dict(os.environ,{"BIOME_STORAGE_PREMOUNTED":"1","BIOME_REPLICA_HOST":"192.168.1.23"}), \
                mock.patch.object(self.backup,"prepare") as attest:
            cfg=self.backup.replica_preflight()
        attest.assert_called_once_with()
        self.assertEqual(cfg["hosts"],["192.168.1.23"])
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),["replica.json"])
        self.ssh_mock.assert_not_called()
    def test_ssh_retains_exact_existing_identity_and_strict_alias(self):
        argv=self.backup.ssh_command("192.168.1.23")
        self.assertEqual(argv[argv.index("-i")+1],str(self.base/"replica-key"))
        self.assertEqual(argv[argv.index("-F")+1],"/dev/null")
        for option in ("BatchMode=yes","IdentitiesOnly=yes","StrictHostKeyChecking=yes",
                       "UserKnownHostsFile="+str(self.base/"known_hosts"),"HostKeyAlias=biome-sacred-pi",
                       "GlobalKnownHostsFile=/dev/null","UpdateHostKeys=no"):
            self.assertIn(option,argv)
        self.assertEqual(argv[-2:],["pi@192.168.1.23","snapshot"])
        self.ssh_mock.assert_not_called()
    def test_replica_plan_requires_current_literal_ip_before_host_commands(self):
        h=load_module("replica_handoff_fixture",ROOT/"deploy/workers/general/handoff.py")
        with mock.patch.object(h,"run",side_effect=AssertionError("No host command")) as run:
            for endpoint in (None,"","stale-host.local","192.168.1.23 extra"):
                with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):
                    h.prepare(SimpleNamespace(component="replica",replica_host=endpoint))
        run.assert_not_called()

if __name__=="__main__":unittest.main(verbosity=2)
