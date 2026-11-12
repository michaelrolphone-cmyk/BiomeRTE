import importlib.util
import json
from pathlib import Path
import plistlib
import sqlite3
import subprocess
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
            plist.parent.mkdir(parents=True);plist.write_bytes(plistlib.dumps(dict(Label="org.biomerte.rover-motion-provider",ProgramArguments=[sys.executable,"old-adapter"])))
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
                    return subprocess.CompletedProcess(argv,0,"","")
                run.side_effect=simulate
                with self.assertRaisesRegex(RuntimeError,"bootstrap failed"):
                    upgrade.upgrade(path.parents[2],state)
                self.assertEqual(original,(config.read_bytes(),plist.read_bytes()))
                self.assertEqual(run.call_args_list[-1].args[0][:2],["launchctl","bootstrap"])

    def test_bootstrap_reconciles_transient_unload_race(self):
        with tempfile.TemporaryDirectory() as directory:
            plist=Path(directory)/"worker.plist"
            plist.write_bytes(plistlib.dumps(dict(ProgramArguments=[sys.executable,"new-adapter"])))
            values=[subprocess.CompletedProcess([],5,"","unloading"),
                    subprocess.CompletedProcess([],113,"","not found"),
                    subprocess.CompletedProcess([],0,"","")]
            with patch.object(upgrade.subprocess,"run",side_effect=values) as run,patch.object(upgrade.time,"sleep"):
                upgrade.bootstrap("gui/501",plist)
                self.assertEqual(run.call_count,3)

    def test_wait_unloaded_waits_for_old_service_removal(self):
        with patch.object(upgrade.subprocess,"run",side_effect=[subprocess.CompletedProcess([],0,"state = exiting", ""),subprocess.CompletedProcess([],113,"", "not found")]) as run,patch.object(upgrade.time,"sleep") as sleep:
            upgrade.wait_unloaded('gui/501','org.biomerte.rover-motion')
            self.assertEqual(run.call_count,2);sleep.assert_called_once()
