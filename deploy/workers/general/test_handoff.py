"""Focused ownership-transfer tests. Every host command is mocked."""
import copy
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location("general_handoff",Path(__file__).with_name("handoff.py"))
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
IMAGE="sha256:"+"a"*64

class HandoffRisks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.guard=mock.patch.object(h,"run",side_effect=AssertionError("Unmocked host command"))
        self.command=self.guard.start();self.addCleanup(self.guard.stop)
    def proof(self):
        return dict(schema="biomerte.native-bind-owner-proof/1",image_id=IMAGE,container_user="0:0",
            native_uid=501,native_gid=20,expected_native_uid=501,expected_native_gid=20,
            mode="0o600",readback_ok=True,passed=True)
    def plan(self,component="storage"):
        d=self.root/component;d.mkdir();state=d/"state";state.mkdir()
        entries=[]
        for index,label in enumerate(h.COMPONENTS[component]["labels"]):
            p=d/(label+".plist");p.write_bytes(plistlib.dumps({"Label":label,"ProgramArguments":["python3","/fixture/"+label+".py"]}))
            entries.append(dict(label=label,path=str(p),sha256=h.digest(p),paths=["/fixture/"+label+".py"],
                                was_loaded=True,was_disabled=False))
        user="0:0" if component in h.ADMINISTRATIVE else "501:20"
        config={"services":{name:{"image":IMAGE,"pull_policy":"never","user":user,
            "volumes":[{"type":"bind","source":str(state),"target":"/state","bind":{"create_host_path":False}}]}
            for name in h.services(component)}}
        h.save(d/"compose.json",config);(d/"environment").write_text("FIXTURE=1\n")
        prepared={n:h.digest(d/n) for n in ("compose.json","environment")}
        if component in h.ADMINISTRATIVE:
            h.save(d/"native-bind-owner-proof.json",self.proof())
            prepared["native-bind-owner-proof.json"]=h.digest(d/"native-bind-owner-proof.json")
        value=dict(format=2,component=component,home=str(self.root),uid=501,gid=20,state=str(state),
            native=entries,native_paths=[p for e in entries for p in e["paths"]],compose=str(d/"compose.json"),
            environment=str(d/"environment"),prepared_files=prepared,image=IMAGE,directory=str(d),
            client_dir=str(d/"client"),status="prepared",containers=[],transitions=[],native_transition_started=False)
        h.persist(value);return value
    def obj(self,plan,name=None,running=False):
        name=name or h.services(plan["component"])[0]
        return {"Id":"container-"+name,"Image":IMAGE,"Config":{"User":"0:0" if plan["component"] in h.ADMINISTRATIVE else "501:20",
            "Labels":{"com.docker.compose.project":"biomerte-workers","com.docker.compose.service":name}},
            "State":{"Running":running,"StartedAt":"2000-01-01T00:00:00Z","Health":{"Status":"healthy"}}}
    def ledger(self,plan,state="uncertain"):
        with sqlite3.connect(Path(plan["state"])/"worker.sqlite") as db:
            db.execute("CREATE TABLE IF NOT EXISTS actions(id TEXT,state TEXT)")
            db.execute("INSERT INTO actions VALUES('original-operation',?)",(state,))
    def test_launchd_disabled_words_and_boolean_values_match_exact_label_and_value(self):
        label="org.biomerte.mac-replica"
        for value,expected in (("disabled",True),("enabled",False),("true",True),("false",False),
                               ("disabled_extra",False),("true_extra",False)):
            with self.subTest(value=value):
                output='\n\tdisabled services = {\n\t\t"org.biomerte.mac-replica-other" => disabled\n'
                output+='\t\t"orgXbiomerteXmac-replica" => disabled\n'
                output+='\t\t"'+label+'" => '+value+'\n\t}\n'
                def host(argv,check=True,env=None):
                    self.assertEqual(argv[0],"/bin/launchctl")
                    if argv[1]=="print-disabled":
                        self.assertEqual(argv[2],"gui/501")
                        return subprocess.CompletedProcess(argv,0,output,"")
                    self.assertEqual(argv[1:],["print","gui/501/"+label])
                    return subprocess.CompletedProcess(argv,0,"loaded","")
                self.command.side_effect=host
                self.assertEqual(h.launch_state(501,label),{"was_loaded":True,"was_disabled":expected})
        self.command.side_effect=lambda argv,check=True,env=None:subprocess.CompletedProcess(
            argv,0 if argv[1]=="print-disabled" else 113,'\n\tdisabled services = {\n\t}\n',"")
        self.assertEqual(h.launch_state(501,label),{"was_loaded":False,"was_disabled":False})
    def test_disabled_or_unloaded_replica_cannot_be_implicitly_activated(self):
        for state in ({"was_loaded":True,"was_disabled":True},{"was_loaded":False,"was_disabled":False}):
            with self.subTest(state=state):
                plan=self.plan("replica");plan["native"][0].update(state)
                with mock.patch.object(h,"controller_preflight") as preflight,mock.patch.object(h,"inspect_containers") as inspected:
                    with self.assertRaisesRegex(RuntimeError,"do not activate implicitly"):h.execute(plan,"apply")
                preflight.assert_not_called();inspected.assert_not_called();self.command.assert_not_called()
                self.assertFalse(plan["native_transition_started"])
                shutil.rmtree(Path(plan["directory"]))
    def test_freeze_pins_image_and_existing_mounts_and_escapes_interpolation(self):
        folder=self.root/"existing";folder.mkdir()
        source={"services":{"storage":{"image":"mutable:tag","build":{"context":"."},"profiles":["storage"],
            "user":"501:20","environment":{"LITERAL":"$EXTERNAL"},"volumes":[{"type":"bind","source":str(folder),"target":"/state"}]},
            "unrelated":{"image":"other"}}}
        frozen=h.freeze_configuration(source,"storage",IMAGE)
        item=frozen["services"]["storage"]
        self.assertEqual(set(frozen["services"]),{"storage"})
        self.assertEqual(item["image"],IMAGE);self.assertEqual(item["pull_policy"],"never")
        self.assertNotIn("build",item);self.assertNotIn("profiles",item)
        self.assertIs(item["volumes"][0]["bind"]["create_host_path"],False)
        self.assertEqual(item["environment"]["LITERAL"],"$$EXTERNAL")
        self.command.assert_not_called()
    def test_prepared_environment_or_compose_drift_refused(self):
        plan=self.plan();h.verify_plan(plan)
        Path(plan["environment"]).write_text("CHANGED=1\n")
        with self.assertRaisesRegex(RuntimeError,"Prepared configuration changed"):h.verify_plan(plan)
        self.command.assert_not_called()
    def test_root_execution_requires_matching_native_owner_proof(self):
        h.verify_bind_proof(self.proof(),501,20)
        for field,value in (("native_uid",0),("native_gid",0),("readback_ok",False),("passed",False)):
            with self.subTest(field=field),self.assertRaises(ValueError):
                h.verify_bind_proof({**self.proof(),field:value},501,20)
    def test_unsettled_includes_monitor_journals_and_partial_replica(self):
        plan=self.plan("workspaces");root=Path(plan["state"])
        for name,value in (("effect",{"active_operation":"existing-exec","state":"active"}),
                           ("checkpoint",{"state":"checkpointing"}),("dispose",{"state":"disposing"}),
                           ("idle",{"state":"active"})):
            d=root/"instances"/name;d.mkdir(parents=True);(d/"state.json").write_text(json.dumps(value))
        (root/".sync-interrupted").mkdir()
        rows=h.unsettled(root)
        self.assertEqual({r.get("workspace") for r in rows if "workspace" in r},{"effect","checkpoint","dispose"})
        self.assertTrue(any("replica_transfer" in r for r in rows))
    def test_controller_failure_happens_before_container_or_native_mutation(self):
        plan=self.plan()
        with mock.patch.object(h,"launch_state",return_value={"was_loaded":True,"was_disabled":False}),mock.patch.object(
                h,"controller_preflight",side_effect=ConnectionError("controller offline")),mock.patch.object(h,"inspect_containers") as inspected:
            with self.assertRaises(ConnectionError):h.execute(plan,"apply")
        inspected.assert_not_called();self.command.assert_not_called()
        self.assertEqual(plan["status"],"preflight_failed")
    def test_created_container_image_mismatch_never_stops_native(self):
        plan=self.plan();bad=self.obj(plan);bad["Image"]="sha256:"+"b"*64
        self.command.side_effect=lambda argv,check=True,env=None: subprocess.CompletedProcess(argv,0,json.dumps([bad]),"")
        responses=[subprocess.CompletedProcess([],0,"",""),subprocess.CompletedProcess([],0,"",""),
                   subprocess.CompletedProcess([],0,bad["Id"]+"\n","")]
        with mock.patch.object(h,"compose",side_effect=responses),mock.patch.object(
                h,"launch_state",return_value={"was_loaded":True,"was_disabled":False}),mock.patch.object(h,"controller_preflight"):
            with self.assertRaisesRegex(RuntimeError,"identity mismatch"):h.execute(plan,"apply")
        self.assertTrue(all(call.args[0][0]==h.DOCKER for call in self.command.call_args_list))
        self.assertFalse(plan["native_transition_started"])
    def test_explicit_rollback_refuses_uncertain_custody_before_stop(self):
        plan=self.plan();self.ledger(plan)
        with self.assertRaisesRegex(RuntimeError,"reconciliation"):h.execute(plan,"rollback")
        self.command.assert_not_called();self.assertEqual(plan["status"],"requires_reconciliation")
    def test_rollback_rechecks_custody_after_container_stop(self):
        plan=self.plan();obj=self.obj(plan,running=True);plan["containers"]=[obj["Id"]]
        stopped=copy.deepcopy(obj);stopped["State"]["Running"]=False
        def stop(argv,check=True,env=None):
            self.assertEqual(argv[:3],[h.DOCKER,"stop","--time"]);self.ledger(plan)
            return subprocess.CompletedProcess(argv,0,"","")
        self.command.side_effect=stop
        with mock.patch.object(h,"inspect_containers",side_effect=[[obj],[stopped]]),mock.patch.object(h,"restore_native") as restored:
            with self.assertRaisesRegex(RuntimeError,"reconciliation"):h.execute(plan,"rollback")
        restored.assert_not_called();self.assertEqual(plan["status"],"requires_reconciliation")
    def test_rollback_will_not_stop_a_replaced_container_owner(self):
        plan=self.plan();plan["containers"]=["original"]
        with mock.patch.object(h,"compose",return_value=subprocess.CompletedProcess([],0,"replacement\n","")):
            with self.assertRaisesRegex(RuntimeError,"ownership set changed"):h.restore(plan)
        self.command.assert_not_called()
    def test_original_unloaded_disabled_job_is_not_bootstrapped(self):
        plan=self.plan();entry=plan["native"][0];entry.update(was_loaded=False,was_disabled=True)
        current={"was_loaded":False,"was_disabled":True}
        self.command.side_effect=lambda argv,check=True,env=None:subprocess.CompletedProcess(argv,0,"","")
        with mock.patch.object(h,"launch_state",return_value=current):h.restore_native(plan)
        commands=[call.args[0] for call in self.command.call_args_list]
        self.assertEqual([a[1] for a in commands],["disable"])
    def test_partial_bootout_reply_loss_restores_original_owner_and_journals_intent(self):
        plan=self.plan("workspaces")
        states={e["label"]:{"was_loaded":True,"was_disabled":False} for e in plan["native"]}
        objects=[self.obj(plan,name) for name in h.services(plan["component"])]
        observations=iter([[],objects,objects,objects])
        failed=[False]
        def host(argv,check=True,env=None):
            self.assertEqual(argv[0],"/bin/launchctl")
            action=argv[1];label=argv[-1].rsplit("/",1)[-1]
            if action=="bootstrap":label=Path(argv[-1]).stem
            if action=="disable":states[label]["was_disabled"]=True
            elif action=="enable":states[label]["was_disabled"]=False
            elif action=="bootstrap":states[label]["was_loaded"]=True
            elif action=="bootout":
                states[label]["was_loaded"]=False
                if not failed[0]:failed[0]=True;raise OSError("bootout reply lost")
            else:self.fail("Unexpected command: "+str(argv))
            return subprocess.CompletedProcess(argv,0,"","")
        self.command.side_effect=host
        with mock.patch.object(h,"controller_preflight"),mock.patch.object(h,"compose",return_value=subprocess.CompletedProcess([],0,"","")),mock.patch.object(
                h,"inspect_containers",side_effect=lambda *args,**kwargs:next(observations)),mock.patch.object(
                h,"launch_state",side_effect=lambda uid,label:dict(states[label])),mock.patch.object(h,"active_processes",return_value=[]):
            with self.assertRaisesRegex(OSError,"reply lost"):h.execute(plan,"apply")
        self.assertEqual(plan["status"],"native_restored")
        self.assertTrue(all(s=={"was_loaded":True,"was_disabled":False} for s in states.values()))
        self.assertTrue(any(t["action"]=="bootout" and t["phase"]=="intent" for t in plan["transitions"]))
        self.assertFalse(plan["native_transition_started"])
    def test_reapplying_healthy_plan_does_not_rollback_or_change_owner(self):
        plan=self.plan();plan.update(status="container_healthy",native_transition_started=True)
        with mock.patch.object(h,"restore") as restored:
            with self.assertRaisesRegex(RuntimeError,"already transitioned"):h.execute(plan,"apply")
        restored.assert_not_called();self.command.assert_not_called()
        self.assertEqual(plan["status"],"container_healthy")

    def test_replica_health_requires_marker_after_this_container_started(self):
        plan=self.plan("replica");obj=self.obj(plan,running=True)
        self.command.side_effect=lambda argv,check=True,env=None:subprocess.CompletedProcess(argv,0,json.dumps({"state":"online","last_poll":946684799}),"")
        self.assertFalse(h.fresh_health(plan,[obj]))
        self.command.side_effect=lambda argv,check=True,env=None:subprocess.CompletedProcess(argv,0,json.dumps({"state":"online","last_poll":946684801}),"")
        self.assertTrue(h.fresh_health(plan,[obj]))
        self.assertGreaterEqual(h.ready_seconds("replica"),720)
        compose=Path(__file__).with_name("compose.yaml").read_text()
        self.assertNotIn("replica-health.json",compose)
        self.assertIn("BIOME_WORKER_HEALTH",compose)

if __name__=="__main__":unittest.main(verbosity=2)
