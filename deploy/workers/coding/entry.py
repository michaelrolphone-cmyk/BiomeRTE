"""Launch the actual coding MCP with explicit durable mounts and host owner."""
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

APP=Path(__file__).resolve().parent
SOURCE=APP/"source"

def validate():
    names=("BIOME_CODING_HOME","BIOME_GITLAB_HOME","BIOME_WORKSPACE_HOME")
    roots={}
    for name in names:
        value=os.environ.get(name)
        if not value or not Path(value).is_absolute():
            raise ValueError(name+" must preserve an explicit absolute state path")
        roots[name]=Path(value)
    coding=roots["BIOME_CODING_HOME"]
    config=json.loads((coding/"coding-config.json").read_text())
    workspace=Path(config["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir() or workspace.resolve()!=workspace:
        raise ValueError("Mount the canonical existing workspace at its unchanged absolute path")
    if not (coding/"mcp-token").is_file() or not (coding/"commands").is_dir():
        raise ValueError("Existing MCP token and durable commands directory are required")
    if not (roots["BIOME_GITLAB_HOME"]/"catalog.json").is_file() or not (roots["BIOME_GITLAB_HOME"]/"agent-commands").is_dir():
        raise ValueError("Existing Git tool catalog and durable command directory are required")
    for name in ("schemas.json","provider.json"):
        if not (roots["BIOME_WORKSPACE_HOME"]/name).is_file():
            raise ValueError("Existing workspace provider metadata is required")
    if not config.get("container") and not (config.get("terminal_tool_path") or os.environ.get("BIOME_TERMINAL_TOOL_PATH")):
        raise ValueError("Native terminal mode requires its existing host tool PATH explicitly")
    path=os.environ.get("BIOME_GATEWAY_HOST_IO_CONFIG")
    if not path or not Path(path).is_file():
        raise ValueError("Existing authenticated HostIO client configuration is required")
    host=json.loads(Path(path).read_text())
    if urlsplit(host.get("url","")).scheme!="https":
        raise ValueError("Coding gateway traffic requires the supplied HTTPS HostIO owner")
    for name in ("ca_file","token_file"):
        if not host.get(name) or not Path(host[name]).is_file():
            raise ValueError("Supplied HostIO CA and token mounts are required")
    if not host.get("host_id"):
        raise ValueError("The registered HostIO host identity must be retained")
    return {"workspace":str(workspace),"mcp_port":config["mcp_port"],"state_paths_preserved":True}

def check():
    sys.path[:0]=[str(SOURCE),str(APP/"gateway"),str(APP/"workspaces"),str(APP)]
    import coding_mcp, container_runner, agent_tools, workspace_agent_tools
    from biomerte.host_io_client import HostIOClient
    subprocess.run(["node","--check",str(SOURCE/"coding_http.mjs")],check=True)
    subprocess.run(["node","--input-type=module","-e",
        "await import('./coding-deps/node_modules/@modelcontextprotocol/server/dist/index.mjs');"
        "await import('./coding-deps/node_modules/@modelcontextprotocol/server-filesystem/dist/server.js');"],
        cwd=str(SOURCE),check=True)
    lock=json.loads((SOURCE/"coding-deps/package-lock.json").read_text())
    return {"implementation":"coding_http.mjs + coding_mcp.py + container_runner.py",
        "tools":[tool["name"] for tool in coding_mcp.TOOLS],
        "filesystem_sdk":lock["packages"]["node_modules/@modelcontextprotocol/server-filesystem"]["version"],
        "mcp_sdk":lock["packages"]["node_modules/@modelcontextprotocol/server"]["version"],
        "credentials_read":False,"provider_started":False,"native_model_engine_included":False}

def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "check"
    if mode=="check": print(json.dumps(check(),sort_keys=True));return
    value=validate()
    if mode=="check-config": print(json.dumps(value,sort_keys=True));return
    if mode!="serve": raise SystemExit("Expected check, check-config, or serve")
    os.umask(0o077)
    os.execvp("node",["node",str(SOURCE/"coding_http.mjs")])
if __name__=="__main__":main()
