"""The real Node MCP/official filesystem SDK and Python bridge on loopback only."""
import http.client
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import tempfile
import time
import unittest

SOURCE=Path("/app/source")
class RealFrontend(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.workspace=self.root/"workspace";self.workspace.mkdir()
        (self.workspace/"fixture.txt").write_text("isolated filesystem fixture")
        (self.root/"outside.txt").write_text("outside confinement marker")
        (self.root/"commands").mkdir()
        with socket.socket() as s:
            s.bind(("127.0.0.1",0));self.port=s.getsockname()[1]
        (self.root/"coding-config.json").write_text(json.dumps({"workspace":str(self.workspace),"mcp_port":self.port,"container":{"image":"fixture"}}))
        self.token="f"*64;(self.root/"mcp-token").write_text(self.token)
        env=dict(os.environ,BIOME_CODING_HOME=str(self.root),BIOME_CODING_LISTEN="127.0.0.1")
        self.process=subprocess.Popen(["node",str(SOURCE/"coding_http.mjs")],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if self.process.poll() is not None:
                self.fail("Real frontend exited: "+self.process.stderr.read())
            ready,_,_=select.select([self.process.stdout],[],[],.1)
            if ready and "Biome coding MCP listening" in self.process.stdout.readline():
                return
        self.fail("Fixture frontend did not begin listening")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.process.stdout.close();self.process.stderr.close()

    def request(self,message,headers=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=10)
        supplied={"Host":"127.0.0.1:"+str(self.port),"Authorization":"Bearer "+self.token,
                  "Content-Type":"application/json","Accept":"application/json, text/event-stream"}
        supplied.update(headers or {})
        try:
            conn.request("POST","/mcp",body=json.dumps(message),headers=supplied)
            response=conn.getresponse();return response.status,response.read().decode()
        finally:conn.close()

    def rpc(self,method,params=None):
        status,body=self.request({"jsonrpc":"2.0","id":1,"method":method,"params":params or {}})
        self.assertEqual(status,200,body)
        value=json.loads(body);self.assertNotIn("error",value,value)
        return value["result"]

    def test_host_origin_and_bearer_boundaries(self):
        body={"jsonrpc":"2.0","id":1,"method":"tools/list"}
        self.assertEqual(self.request(body,{"Host":"attacker.invalid"})[0],403)
        self.assertEqual(self.request(body,{"Origin":"http://attacker.invalid"})[0],403)
        self.assertEqual(self.request(body,{"Authorization":"Bearer invalid"})[0],401)
        result=self.rpc("tools/list")
        names={tool["name"] for tool in result["tools"]}
        self.assertTrue({"start_command","command_status","git_tools","git_start","git_status",
                         "workspace_tools","workspace_start","workspace_status"}.issubset(names))

    def test_real_official_filesystem_reads_and_confinement(self):
        names={tool["name"] for tool in self.rpc("tools/list")["tools"]}
        read="read_text_file" if "read_text_file" in names else "read_file"
        value=self.rpc("tools/call",{"name":read,"arguments":{"path":str(self.workspace/"fixture.txt")}})
        self.assertIn("isolated filesystem fixture",json.dumps(value))
        rejected=self.rpc("tools/call",{"name":read,"arguments":{"path":str(self.root/"outside.txt")}})
        self.assertTrue(rejected.get("isError"),rejected)
        self.assertNotIn("outside confinement marker",json.dumps(rejected))

    def test_real_node_to_python_dispatch_reports_existing_record_error(self):
        value=self.rpc("tools/call",{"name":"command_status","arguments":{"operation":"lmc-missing"}})
        self.assertTrue(value.get("isError"),value)
        self.assertIn("lmc-missing",json.dumps(value))
        self.assertFalse(any((self.root/"commands").iterdir()))

if __name__=="__main__":unittest.main(verbosity=2)
