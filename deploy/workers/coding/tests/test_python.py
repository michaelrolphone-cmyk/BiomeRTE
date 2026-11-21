"""Real durable coding implementations against isolated filesystem/RPC fixtures."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

IN_IMAGE=(Path("/app/source")/"coding_mcp.py").is_file()
if IN_IMAGE:
    APP=Path("/app"); SOURCE=APP/"source"; GATEWAY=APP/"gateway"; WORKSPACES=APP/"workspaces"; REPO=None
else:
    REPO=Path(__file__).resolve().parents[4]
    APP=REPO/"deploy/workers/coding"; SOURCE=APP/"source"
    GATEWAY=REPO/"providers/mac-terminal"; WORKSPACES=REPO/"integrations/workspaces"
os.environ["BIOME_GATEWAY_CODE"]=str(GATEWAY)
os.environ["BIOME_WORKSPACES_CODE"]=str(WORKSPACES)
sys.path[:0]=[str(SOURCE),str(GATEWAY),str(WORKSPACES),str(APP if IN_IMAGE else REPO)]
import coding_mcp as cm
import container_runner as cr
import agent_tools as gt
import workspace_agent_tools as wt
import manager as wm

class CodingImplementation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ("coding/commands","git/agent-commands","workspaces","workspace"):
            (self.root/name).mkdir(parents=True)
        for obj,attribute,value in ((cm,"ROOT",self.root/"coding"),(cr,"ROOT",self.root/"coding"),
                                    (gt,"ROOT",self.root/"git"),(wm,"ROOT",self.root/"workspaces")):
            patch=mock.patch.object(obj,attribute,value);patch.start();self.addCleanup(patch.stop)

    def test_captured_source_and_common_gateway_hashes(self):
        record=json.loads((APP/"source-provenance.json").read_text())
        for item in record["files"]:
            path=SOURCE/item["destination"].split("/source/",1)[1] if IN_IMAGE else REPO/item["destination"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),item["packaged_sha256"])
        for item in record["shared_files"]:
            path=APP/item["image_path"] if IN_IMAGE else REPO/item["repository_path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),item["sha256"])

    def test_lost_submission_persists_original_identity_without_new_effect(self):
        path=cm.record_path("lmc-fixture")
        state={"operation":"lmc-fixture","sequence":0,"pending":None,"lease":"lease.fixture"}
        calls=[]
        def reply(operation,arguments=None,rid=None):
            calls.append((operation,arguments,rid))
            if operation=="cap.invoke": raise OSError("fixture lost submission reply")
            return {"state":"claimed"}
        with mock.patch.object(cr.biome,"invoke",side_effect=reply):
            with self.assertRaises(OSError):
                cr.command_step(path,state,"create",["create","fixture"])
            original=json.loads(path.read_text())
            resumed=json.loads(path.read_text())
            self.assertIsNone(cr.command_step(path,resumed,"create",["create","fixture"]))
        self.assertEqual(original["pending"]["operation"],resumed["pending"]["operation"])
        self.assertEqual(resumed["sequence"],1)
        self.assertEqual([x[0] for x in calls],["cap.invoke","cap.status"])
        self.assertEqual(calls[0][1]["operation"],calls[1][1]["operation"])

    def test_existing_done_record_retains_nonzero_result_and_absolute_artifact_path(self):
        state={"operation":"lmc-finished","backend":"docker","stage":"done","logs":{"stdout":"fixture","stderr":"failed build"},
            "exit_code":7,"image":"fixture-image","resource":"docker.fixture","provider":"p"*64,
            "container":"lmc-finished","container_state":{"Running":False,"ExitCode":7},"snapshot_id":"s"*64,
            "output_artifact":{"local_path":"/original/native/commands/lmc-finished.output.tar"},"cli_operations":["original-command"]}
        cm.save(cm.record_path(state["operation"]),state)
        with mock.patch.object(cr.biome,"invoke",side_effect=AssertionError("Completed record must not submit work")):
            result=cm.command_status({"operation":state["operation"]})
        self.assertEqual(result["operation"],state["operation"])
        self.assertEqual(result["result"]["exit_code"],7)
        self.assertEqual(result["result"]["output_artifact"],state["output_artifact"])
        self.assertEqual(json.loads(cm.record_path(state["operation"]).read_text()),state)

    def test_workspace_archive_preserves_exact_path_and_excludes_results(self):
        root=self.root/"workspace";(root/"source.txt").write_text("fixture-source")
        (root/".biome-results").mkdir();(root/".biome-results/old.txt").write_text("exclude")
        data,snapshot=cr.archive_workspace(root,str(root))
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            names=archive.getnames()
            self.assertIn(str(root).lstrip("/")+"/source.txt",names)
            self.assertFalse(any(".biome-results" in name for name in names))
        self.assertEqual(len(snapshot),64)

    def test_eight_tools_and_existing_workspace_resource(self):
        names={item["name"] for item in cm.dispatch({"method":"tools/list"})["tools"]}
        self.assertEqual(names,{"start_command","command_status","git_tools","git_start","git_status",
                               "workspace_tools","workspace_start","workspace_status"})
        (wm.ROOT/"provider.json").write_text(json.dumps({"resource":"workspace.original"}))
        gateway=mock.Mock()
        gateway.invoke.side_effect=lambda operation,arguments,rid:{"operation":arguments["operation"],"state":"queued"}
        with mock.patch.object(wm,"gateway",return_value=gateway):
            result=wt.workspace_start({"contract":"workspace.status:1","arguments":{"workspace_id":"w"}})
        args=gateway.invoke.call_args
        self.assertEqual(args.args[0],"cap.submit")
        self.assertEqual(args.args[1]["resource"],"workspace.original")
        self.assertEqual(args.args[1]["operation"],result["operation"])
        self.assertEqual(args.args[2],result["operation"]+"-submit")

    def test_common_gateway_uses_supplied_host_owner_and_same_request_id(self):
        cfg=self.root/"host-io.json";cfg.write_text(json.dumps({"url":"https://host.docker.internal:8765"}))
        client=mock.Mock();client.call.return_value={"state":"queued"}
        with mock.patch.dict(os.environ,{"BIOME_GATEWAY_HOST_IO_CONFIG":str(cfg)}), mock.patch(
                "biomerte.host_io_client.HostIOClient",return_value=client), mock.patch.object(
                cm.biome,"credentials",side_effect=AssertionError("Native OAuth must stay with host owner")):
            result=cm.biome.invoke("cap.status",{"operation":"lmc-existing"},"original-request")
        self.assertEqual(result,{"state":"queued"})
        self.assertEqual(client.call.call_args.args[0],"gateway.invoke")
        self.assertEqual(client.call.call_args.args[1]["request_id"],"original-request")
        self.assertEqual(client.call.call_args.args[1]["arguments"],{"operation":"lmc-existing"})

if __name__=="__main__":unittest.main(verbosity=2)
