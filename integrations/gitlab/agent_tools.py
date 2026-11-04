"""Agent-facing facade: all GitLab/Git actions execute through Biome capabilities."""
import json,sys,time,uuid
from pathlib import Path
ROOT=Path.home()/".local/share/biome-gitlab"
sys.path.insert(0,str(Path.home()/".local/share/biome-mac-terminal"))
import mac_provider as biome
def git_tools(args):
 tools=[dict(t,contract="git.gitlab."+t["name"]+":1") for t in json.loads((ROOT/"catalog.json").read_text())]
 return {"mcp_tools":tools,"native_git":["git.cli:1","git.gitlab.api:1","git.mcp.call:1","git.mcp.tools:1"]+["git."+x+":1" for x in ("clone","fetch","pull","push","status","diff","log","commit","checkout","branch","merge","add","init")],"invoke":"Use git_start({contract,arguments}); retain its operation and use git_status until settled. Inspect result.ok."}
def operation_file(operation):
 if not operation.startswith("git-") or not all(x.isalnum() or x=="-" for x in operation):raise ValueError("Invalid Git operation")
 return ROOT/"agent-commands"/(operation+".json")
def git_start(args):
 contract=args["contract"]
 if not contract.startswith("git.") or not contract.endswith(":1"):raise ValueError("Expected Git capability contract")
 operation="git-"+uuid.uuid4().hex;p=operation_file(operation);p.parent.mkdir(parents=True,exist_ok=True)
 biome.save_json(p,{"operation":operation,"contract":contract,"arguments":args.get("arguments",{}),"stage":"acquiring"})
 return git_status({"operation":operation})
def git_status(args):
 p=operation_file(args["operation"]);s=json.loads(p.read_text());op=s["operation"]
 if s["stage"]=="acquiring":
  lease=biome.invoke("cap.acquire",{"contract":s["contract"],"task":op},op+"-acquire");s.update(lease=lease["id"],stage="binding");biome.save_json(p,s)
 if s["stage"]=="binding":
  lease=biome.invoke("cap.lease",{"lease":s["lease"]})
  if lease["state"]!="bound":return {"operation":op,"state":lease["state"]}
  biome.invoke("cap.invoke",{"lease":s["lease"],"operation":op,"inputs":{"arguments":s["arguments"]}},op+"-invoke");s["stage"]="submitted";biome.save_json(p,s)
 result=biome.invoke("cap.status",{"operation":op})
 if result["state"] in ("succeeded","failed","cancelled") and s["stage"]!="released":
  biome.invoke("cap.release",{"lease":s["lease"]},op+"-release");s["stage"]="released";biome.save_json(p,s)
 return {"operation":op,"state":result["state"],"result":result.get("result"),"guidance":"Queued/claimed is pending; inspect result.ok. Uncertain requires reconciliation with the original operation ID."}
TOOLS=[
 {"name":"git_tools","description":"Discover GitLab MCP tools and native Git capabilities available through Biome.","inputSchema":{"type":"object","properties":{}}},
 {"name":"git_start","description":"Start Git/GitLab work through Biome. Returns durable operation ID; use git_status until settled. Tokens stay on Mac.","inputSchema":{"type":"object","properties":{"contract":{"type":"string"},"arguments":{"type":"object"}},"required":["contract","arguments"]}},
 {"name":"git_status","description":"Read a Git operation's durable result. Check actual result.ok, not only lifecycle state.","inputSchema":{"type":"object","properties":{"operation":{"type":"string"}},"required":["operation"]}}
]
