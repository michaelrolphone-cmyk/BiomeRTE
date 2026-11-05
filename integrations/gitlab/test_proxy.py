import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location("proxy",Path(__file__).with_name("proxy.py"));p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
class ProxyTests(unittest.TestCase):
 def test_json_and_sse_mcp_results_preserved(self):
  obj={"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"café"}]}}
  self.assertEqual(obj,p.decode_rpc(json.dumps(obj).encode(),"application/json"))
  raw=('event: message\ndata: '+json.dumps(obj)+'\n\n').encode()
  self.assertEqual(obj,p.decode_rpc(raw,"text/event-stream"))
 def test_mcp_tool_list_pages_and_keeps_schema(self):
  with patch.object(p,"settings",return_value={}):
   c=p.GitLabMCP()
  schema={"type":"object","properties":{"project_id":{"type":"string"}}}
  with patch.object(c,"request",side_effect=[{"tools":[{"name":"get_project","inputSchema":schema}],"nextCursor":"next"},{"tools":[{"name":"save_project","inputSchema":schema}]}]) as call:
   result=c.tools();self.assertEqual(2,len(result));self.assertEqual(schema,result[0]["inputSchema"]);self.assertEqual(("tools/list",{"cursor":"next"}),call.call_args_list[1].args)
 def test_git_workspaces_reject_traversal(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(p,"ROOT",Path(tmp)):
   for args in ({"namespace":"../private"},{"namespace":"demo","path":"../../private"}):
    with self.assertRaises(ValueError):p.workspace(args)
 def test_native_git_init_commit_and_clone(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(p,"ROOT",Path(tmp)),patch.object(p,"settings",return_value={"ca":str(Path(tmp)/"ca.crt")}):
   a={"namespace":"demo","path":"source"}
   self.assertTrue(p.cli("git.init",a)["ok"]);repo=Path(tmp)/"workspaces/demo/source";(repo/"hello.txt").write_text("hello\n")
   self.assertTrue(p.cli("git.add",dict(a,options=["hello.txt"]))["ok"])
   self.assertTrue(p.cli("git.commit",dict(a,options=["-m","Initial commit"]))["ok"])
   self.assertIn("Initial commit",p.cli("git.log",dict(a,options=["--oneline"]))["result"]["stdout"])
   self.assertTrue(p.cli("git.clone",{"namespace":"demo","path":"copy","url":str(repo)})["ok"])
   self.assertEqual("hello\n",(repo.parent/"copy/hello.txt").read_text())
 def test_large_results_artifact_chunks_round_trip(self):
  import base64,hashlib
  parts={}
  class Gateway:
   def invoke(self,name,args):
    self.assert_name=name;raw=base64.b64decode(args["data"]);sha=hashlib.sha256(raw).hexdigest();parts[sha]=raw;return {"sha256":sha,"size":len(raw)}
  result={"content":[{"type":"text","text":"café"*20000}]}
  with patch.object(p,"gateway",return_value=Gateway()):out=p.compact({"ok":True,"result":result})
  artifact=out["result"]["artifact"];raw=b"".join(parts[c["sha256"]] for c in artifact["chunks"])
  self.assertEqual(artifact["sha256"],hashlib.sha256(raw).hexdigest());self.assertEqual(result,json.loads(raw));self.assertGreater(len(artifact["chunks"]),1)
 def test_mcp_error_is_not_success(self):
  raw={"isError":True,"content":[{"type":"text","text":"Project not found"}]}
  with patch.object(p,"GitLabMCP") as cls,patch("sys.stdin",__import__("io").StringIO(json.dumps({"inputs":{"arguments":{}}}))),patch("sys.stdout",new_callable=__import__("io").StringIO) as output:
   cls.return_value.call.return_value=raw;p.adapter("git.gitlab.get_project")
   self.assertFalse(json.loads(output.getvalue())["ok"])
class ApiTests(unittest.TestCase):
 def test_rest_rejects_foreign_origin(self):
  with patch.object(p,"settings",return_value={"url":"https://gitlab.local"}):
   with self.assertRaises(ValueError):p.rest({"path":"https://other.example/api/v4/projects"})
 def test_rest_preserves_list_response(self):
  with patch.object(p,"settings",return_value={"url":"https://gitlab.local","ca":"unused"}),patch.object(p,"token",return_value="test-token"),patch.object(p.ssl,"create_default_context"),patch.object(p.urllib.request,"urlopen") as call:
   response=call.return_value.__enter__.return_value;response.status=200;response.read.return_value=b'[{"id":1}]'
   result=p.rest({"path":"/api/v4/projects"});self.assertTrue(result["ok"]);self.assertEqual([{"id":1}],result["result"]["data"])
if __name__=="__main__":unittest.main()
