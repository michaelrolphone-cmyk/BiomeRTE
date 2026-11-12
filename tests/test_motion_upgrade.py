import importlib.util
import json
from pathlib import Path
import plistlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/"providers/rover-motion/upgrade.py"
spec=importlib.util.spec_from_file_location("motion_upgrade",path)
upgrade=importlib.util.module_from_spec(spec);spec.loader.exec_module(upgrade)


class UpgradeTests(unittest.TestCase):
    def test_active_work_prevents_restart_and_bootstrap_failure_restores_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);state=home/"provider";state.mkdir()
            cfg=dict(runtime=str(home/"old"),python=sys.executable,provider="original",resource="rover")
            config=state/"provider.json";config.write_text(json.dumps(cfg))
            plist=home/"Library/LaunchAgents/org.biomerte.rover-motion-provider.plist"
            plist.parent.mkdir(parents=True);plist.write_bytes(plistlib.dumps(dict(Label="org.biomerte.rover-motion-provider")))
            original=(config.read_bytes(),plist.read_bytes())
            with sqlite3.connect(state/"worker.sqlite") as db:
                db.executescript("CREATE TABLE actions(state TEXT); CREATE TABLE supervised_pending(id TEXT); INSERT INTO actions VALUES('running');")
            with patch.object(upgrade.Path,"home",return_value=home),patch.object(upgrade.subprocess,"run") as run:
                with self.assertRaisesRegex(RuntimeError,"unsettled"):
                    upgrade.upgrade(path.parents[2],state)
                self.assertFalse(any(call.args[0][0]=="launchctl" for call in run.call_args_list))
                with sqlite3.connect(state/"worker.sqlite") as db:db.execute("UPDATE actions SET state='succeeded'")
                run.reset_mock()
                def simulate(argv,**kwargs):
                    if argv[:2]==["launchctl","bootstrap"] and config.read_bytes()!=original[0]:
                        raise RuntimeError("bootstrap failed")
                run.side_effect=simulate
                with self.assertRaisesRegex(RuntimeError,"bootstrap failed"):
                    upgrade.upgrade(path.parents[2],state)
                self.assertEqual(original,(config.read_bytes(),plist.read_bytes()))
                self.assertEqual(run.call_args_list[-1].args[0][:2],["launchctl","bootstrap"])
