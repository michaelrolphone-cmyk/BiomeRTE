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
 def test_mcp_error_is_not_success(self):
  raw={"isError":True,"content":[{"type":"text","text":"Project not found"}]}
  with patch.object(p,"GitLabMCP") as cls,patch("sys.stdin",__import__("io").StringIO(json.dumps({"inputs":{"arguments":{}}}))),patch("sys.stdout",new_callable=__import__("io").StringIO) as output:
   cls.return_value.call.return_value=raw;p.adapter("git.gitlab.get_project")
   self.assertFalse(json.loads(output.getvalue())["ok"])
if __name__=="__main__":unittest.main()
