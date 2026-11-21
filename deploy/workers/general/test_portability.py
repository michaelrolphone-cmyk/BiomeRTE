import importlib.util
import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

class StoragePortability(unittest.TestCase):
    def test_native_preparation_is_never_invoked_in_linux_mode(self):
        v=module("worker_volumes","providers/mac-storage/volumes.py")
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{"BIOME_STORAGE_PREMOUNTED":"1"}):
            v.BASE=Path(d);v.MOUNT=Path(d)/"workspace";v.IMAGE=Path(d)/"missing";v.VAULT=Path(d)/"missing-vault"
            with patch.object(v,"mounted",return_value=False),patch.object(v.subprocess,"run") as run:
                with self.assertRaises(RuntimeError):v.prepare()
                run.assert_not_called()
            self.assertFalse(v.MOUNT.exists())
    def test_linux_reservation_commits_real_blocks(self):
        v=module("worker_volumes","providers/mac-storage/volumes.py")
        with tempfile.TemporaryDirectory() as d,patch.object(v.sys,"platform","linux"):
            p=Path(d)/"reserved";v.allocate(p,1024*1024)
            self.assertEqual(p.stat().st_size,1024*1024)
            self.assertGreaterEqual(p.stat().st_blocks*512,1024*1024)

class NativeStorageIdentity(unittest.TestCase):
    def test_disabled_observation_fails_closed(self):
        from biomerte.storage_host_io import status
        with self.assertRaises(PermissionError):status({})
    def test_substituted_native_identity_is_rejected(self):
        v=module("worker_volumes","providers/mac-storage/volumes.py")
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{"BIOME_STORAGE_PREMOUNTED":"1","BIOME_GATEWAY_HOST_IO_CONFIG":"/not-read"}):
            v.BASE=Path(d);v.MOUNT=Path(d)/"workspace";v.MOUNT.mkdir()
            (v.MOUNT/v.MARK).write_text("expected")
            (v.BASE/"volume.json").write_text(json.dumps({"workspace_uuid":"expected"}))
            with patch.object(v,"native_status",return_value={"mounted":True,"workspace_path":str(v.MOUNT),"workspace_uuid":"substitute"}):
                self.assertFalse(v.mounted())

if __name__=="__main__":unittest.main()
