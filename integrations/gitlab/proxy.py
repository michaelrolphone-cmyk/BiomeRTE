#!/usr/bin/env python3
"""GitLab MCP and native Git provider. Uses Biome's existing durable worker."""
import argparse, base64, fcntl, hashlib, json, os, re, sqlite3, ssl, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path
ROOT=Path(os.environ.get("BIOME_GITLAB_HOME",Path.home()/".local/share/biome-gitlab"))
TERMINAL=Path.home()/".local/share/biome-mac-terminal"
NATIVE=("clone","fetch","pull","push","status","diff","log","commit","checkout","branch","merge","add","init")
def atomic(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_name(path.name+"."+uuid.uuid4().hex);tmp.write_text(json.dumps(value,ensure_ascii=False));tmp.chmod(0o600);os.replace(tmp,path)
def settings():return json.loads((ROOT/"config.json").read_text())
def gateway():
 sys.path.insert(0,str(TERMINAL));import mac_provider;return mac_provider
def token():
 with open(ROOT/"oauth.lock","a") as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);p=ROOT/"gitlab-oauth.json";d=json.loads(p.read_text())
  if time.time()<d["expires_at"]-120:return d["access_token"]
  if (ROOT/"refresh-pending.json").exists():raise RuntimeError("OAuth refresh outcome unresolved; renew GitLab service credentials locally")
  atomic(ROOT/"refresh-pending.json",{"started_at":time.time()})
  cfg=settings();body=urllib.parse.urlencode(dict(grant_type="refresh_token",refresh_token=d["refresh_token"],client_id=d["client_id"],client_secret=d["client_secret"])).encode()
  req=urllib.request.Request(cfg["url"]+"/oauth/token",data=body,headers={"Content-Type":"application/x-www-form-urlencoded"})
  with urllib.request.urlopen(req,context=ssl.create_default_context(cafile=cfg["ca"])) as r:new=json.load(r)
  d.update(access_token=new["access_token"],refresh_token=new["refresh_token"],expires_at=time.time()+new["expires_in"]);atomic(p,d);(ROOT/"refresh-pending.json").unlink();return d["access_token"]
def decode_rpc(raw,ctype):
 if "text/event-stream" in ctype:
  messages=[]
  for event in raw.decode().replace("\r\n","\n").split("\n\n"):
   data="\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
   if data:
    obj=json.loads(data)
    if "id" in obj:messages.append(obj)
  if not messages:raise RuntimeError("MCP stream ended without a correlated response")
  return messages[-1]
 return json.loads(raw)
class GitLabMCP:
 def __init__(self):self.cfg=settings();self.session=None;self.counter=0
 def request(self,method,params=None,notification=False):
  self.counter+=1;rid=self.counter
  obj={"jsonrpc":"2.0","method":method,"params":params or {}}
  if not notification:obj["id"]=rid
  h={"Content-Type":"application/json","Accept":"application/json, text/event-stream","Authorization":"Bearer "+token(),"MCP-Protocol-Version":"2025-06-18","X-Gitlab-Enabled-Mcp-Server-Toolsets":"core,merge_requests,work_items,repository,ci,wikis"}
  if self.session:h["Mcp-Session-Id"]=self.session
  req=urllib.request.Request(self.cfg["url"]+"/api/v4/mcp",data=json.dumps(obj).encode(),headers=h)
  try:
   with urllib.request.urlopen(req,context=ssl.create_default_context(cafile=self.cfg["ca"])) as r:
    self.session=r.headers.get("Mcp-Session-Id",self.session);raw=r.read()
    if notification:return None
    result=decode_rpc(raw,r.headers.get("Content-Type",""))
  except urllib.error.HTTPError as e:raise RuntimeError("GitLab MCP HTTP "+str(e.code)) from None
  if result.get("id")!=rid:raise RuntimeError("MCP response identity mismatch")
  if "error" in result:raise RuntimeError("GitLab MCP protocol error "+str(result["error"].get("code")))
  return result["result"]
 def initialize(self):
  self.request("initialize",{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"Biome GitLab capability proxy","version":"1.0.0"}});self.request("notifications/initialized",notification=True)
 def tools(self):
  out=[];cursor=None
  while True:
   page=self.request("tools/list",{"cursor":cursor} if cursor else {});out.extend(page.get("tools",[]));cursor=page.get("nextCursor")
   if not cursor:return out
 def call(self,name,args):return self.request("tools/call",{"name":name,"arguments":args})
def tool_contract(name):
 if not re.fullmatch(r"[A-Za-z0-9_.-]+",name):raise ValueError("Unsupported MCP tool identifier")
 return "git.gitlab."+name+":1"
def workspace(args):
 ns=args.get("namespace","default")
 if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}",ns):raise ValueError("Invalid workspace namespace")
 root=(ROOT/"workspaces"/ns).resolve();root.mkdir(parents=True,exist_ok=True)
 target=(root/args.get("path",".")).resolve()
 if not target.is_relative_to(root):raise ValueError("path must be inside the workspace namespace")
 return root,target
def cli(tool,args):
 root,cwd=workspace(args);kind=tool.removeprefix("git.")
 argv=args.get("argv",[]) if kind=="cli" else [kind]+args.get("options",[])
 if not isinstance(argv,list) or any(not isinstance(x,str) for x in argv):raise ValueError("argv/options must be a list of strings")
 if kind=="clone":
  if cwd==root or cwd.exists():raise ValueError("Clone requires a new destination path")
  cwd.parent.mkdir(parents=True,exist_ok=True);argv=["clone"]+args.get("options",[])+[args["url"],str(cwd)];run_dir=cwd.parent
 else:
  cwd.mkdir(parents=True,exist_ok=True);run_dir=cwd
 # The repository lock also serializes different leases touching the same worktree.
 locks=ROOT/"locks";locks.mkdir(exist_ok=True)
 with open(locks/(hashlib.sha256(str(cwd).encode()).hexdigest()+".lock"),"a") as lock:
  fcntl.flock(lock,fcntl.LOCK_EX);cfg=settings()
  helper=str(ROOT/"credential_helper.sh")
  command=["/usr/bin/git","-c","credential.helper=","-c","credential.helper="+helper,"-c","http.sslCAInfo="+cfg["ca"],"-c","user.name=Biome Agent","-c","user.email=biome-agent@biome.local"]+argv
  env=dict(os.environ,GIT_TERMINAL_PROMPT="0")
  p=subprocess.run(command,cwd=run_dir,env=env,input=args.get("stdin","").encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  return {"ok":p.returncode==0,"result":{"exit_code":p.returncode,"stdout":p.stdout.decode("utf-8","replace"),"stderr":p.stderr.decode("utf-8","replace"),"workspace":str(cwd)}}
def credential():
 values={}
 for line in sys.stdin:
  if "=" in line:k,v=line.rstrip("\n").split("=",1);values[k]=v
 cfg=settings();origin=urllib.parse.urlsplit(cfg["url"])
 if values.get("protocol")==origin.scheme and values.get("host")==origin.netloc:
  print("username=oauth2\npassword="+token()+"\n")
def compact(out):
 raw=json.dumps(out["result"],ensure_ascii=False).encode()
 if len(raw)<=48000:return out
 chunks=[];g=gateway()
 for offset in range(0,len(raw),32768):
  part=raw[offset:offset+32768];r=g.invoke("artifact.put",{"data":base64.b64encode(part).decode()});chunks.append({"sha256":r["id"],"size":len(part)})
 return {"ok":out["ok"],"result":{"artifact":{"sha256":hashlib.sha256(raw).hexdigest(),"size":len(raw),"chunks":chunks},"encoding":"json"}}
def rest(args):
 cfg=settings();path=args.get("path","")
 if not path.startswith("/api/v4/") or urllib.parse.urlsplit(path).netloc:raise ValueError("Expected a relative /api/v4/ path")
 method=args.get("method","GET").upper();body=args.get("body")
 req=urllib.request.Request(cfg["url"]+path,method=method,data=json.dumps(body).encode() if body is not None else None,headers={"Authorization":"Bearer "+token(),"Content-Type":"application/json","Accept":"application/json"})
 try:
  with urllib.request.urlopen(req,context=ssl.create_default_context(cafile=cfg["ca"])) as r:
   raw=r.read();value=json.loads(raw) if raw else {}
   return {"ok":True,"result":{"status":r.status,"data":value}}
 except urllib.error.HTTPError as e:
  return {"ok":False,"result":{"status":e.code,"error":e.read(4096).decode("utf-8","replace")}}
def adapter(tool):
 payload=json.load(sys.stdin);args=payload["inputs"]["arguments"]
 if tool=="git.mcp.tools":out={"ok":True,"result":{"tools":json.loads((ROOT/"catalog.json").read_text())}}
 elif tool=="git.mcp.call":
  client=GitLabMCP();client.initialize();raw=client.call(args["tool"],args.get("arguments",{}));out={"ok":not raw.get("isError",False),"result":raw}
 elif tool=="git.gitlab.api":out=rest(args)
 elif tool.startswith("git.gitlab."):
  client=GitLabMCP();client.initialize();raw=client.call(tool[len("git.gitlab."):],args);out={"ok":not raw.get("isError",False),"result":raw}
 else:out=cli(tool,args)
 print(json.dumps(compact(out),ensure_ascii=False))
def configure():
 g=gateway();cfg=settings();info=g.invoke("system.info",{})
 nodefile=ROOT/"provider.json"
 if nodefile.exists():node=json.loads(nodefile.read_text())["node"]
 else:node=hashlib.sha256(os.urandom(32)).hexdigest();atomic(nodefile,{"node":node})
 client=GitLabMCP();client.initialize();tools=client.tools();atomic(ROOT/"catalog.json",tools)
 entries=[("git.gitlab.api","GitLab REST API for operations absent from native MCP. arguments {method,path,body?}, path starts /api/v4/."),("git.mcp.tools","Discover the current native GitLab MCP tool schemas."),("git.mcp.call","Call a native GitLab MCP tool with {tool,arguments}. Inspect ok and result.isError."),("git.cli","Run Git argv in a namespaced Mac worktree; arguments {namespace,path,argv,stdin?}.")]
 entries.extend(("git."+n,"Native git "+n+"; arguments {namespace,path,options?,url?}. clone requires url and a new path.") for n in NATIVE)
 entries.extend((tool_contract(t["name"]).rsplit(":",1)[0],t.get("description","GitLab MCP tool")) for t in tools)
 prior=json.loads((ROOT/"manifest.json").read_text()) if (ROOT/"manifest.json").exists() else {}
 known={o["contract"] for o in prior.get("offers",[])}
 offers=[];resource="git.mac."+node[:20]
 for name,desc in entries:
  contract=name+":1";tool=next((t for t in tools if tool_contract(t["name"])==contract),None)
  usage={"before":desc,"parameters":{"arguments":"Native MCP inputSchema is published in the tool catalog. Native Git uses namespace, path and argv/options; tokens stay on the Mac."},"read_result":"Check result.ok. For MCP inspect result.result (including isError/content/structuredContent). Large JSON is losslessly returned as artifact chunks; decode and verify SHA256."}
  if tool:usage["before"]+=" Exact inputSchema: call git.mcp.tools:1 and select "+tool["name"]+"."
  if contract not in known:g.invoke("cap.define",dict(name=name,inputs={"arguments":"obj"},outputs={"ok":"bool","result":"obj"},description=("GitLab MCP tool "+tool["name"] if tool else desc[:1000]),usage=usage))
  offers.append(dict(contract=contract,resource=resource,service="gitlab-mcp-proxy",capacity=16,usage=usage,argv=[sys.executable,str(ROOT/"proxy.py"),"adapter",name]))
 manifest={"site":info["site"],"realm":"site","offers":offers,"parallelism":16,"timeout":None,"poll_seconds":2,"retry_seconds":5}
 atomic(ROOT/"manifest.json",manifest)
 print(json.dumps({"provider":node,"resource":resource,"mcp_tools":len(tools),"contracts":len(offers),"url":cfg["url"]}))
def worker():
 sys.path.insert(0,str(TERMINAL/"runtime"));from biomerte.worker import run_worker
 cfg=json.loads((ROOT/"manifest.json").read_text());node=json.loads((ROOT/"provider.json").read_text())["node"];g=gateway()
 class Remote:
  site=cfg["site"];realm="site"
  def __init__(self):self.node=node
  def call(self,name,args,rid=None):return g.invoke(name,args,rid,self.realm)
 run_worker(str(ROOT),Remote(),cfg,False)
def main():
 mode=sys.argv[1]
 if mode=="configure":configure()
 elif mode=="worker":worker()
 elif mode=="adapter":adapter(sys.argv[2])
 elif mode=="credential":credential()
 else:raise ValueError("Unknown mode")
if __name__=="__main__":
 try:main()
 except Exception as exc:
  print(type(exc).__name__+": "+str(exc)[:500],file=sys.stderr);sys.exit(1)
