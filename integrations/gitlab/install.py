#!/usr/bin/env python3
"""Install the already-authorized Mac GitLab capability provider; preserve credentials/data."""
import json,os,pathlib,plistlib,shutil,subprocess,sys
root=pathlib.Path.home()/".local/share/biome-gitlab";root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
src=pathlib.Path(__file__).resolve().parent
shutil.copy2(src/"proxy.py",root/"proxy.py")
shutil.copy2(src/"agent_tools.py",root/"agent_tools.py")
shutil.copytree(src.parent.parent/"biomerte",root/"runtime/biomerte",dirs_exist_ok=True)
url="https://Micahels-Mac-mini.local:8443"
coding_config=pathlib.Path.home()/".local/share/biome-lmstudio/coding-config.json"
workspace_root=pathlib.Path(json.loads(coding_config.read_text())["workspace"])/"git" if coding_config.exists() else root/"workspaces"
workspace_root.mkdir(parents=True,exist_ok=True)
(root/"config.json").write_text(json.dumps({"url":url,"ca":str(root/"tls/gitlab.crt"),"workspace_root":str(workspace_root)}))
python=sys.executable
(root/"credential_helper.sh").write_text('#!/bin/sh\n[ "$1" = get ] || exit 0\nexec '+__import__('shlex').quote(python)+' '+__import__('shlex').quote(str(root/"proxy.py"))+' credential\n')
(root/"credential_helper.sh").chmod(0o700)
label="org.biomerte.gitlab-provider"
plist={"Label":label,"ProgramArguments":[python,str(root/"proxy.py"),"worker"],"RunAtLoad":True,"KeepAlive":True,"WorkingDirectory":str(root),"EnvironmentVariables":{"PATH":"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},"StandardOutPath":str(root/"provider.log"),"StandardErrorPath":str(root/"provider-error.log")}
path=pathlib.Path.home()/"Library/LaunchAgents"/(label+".plist");path.parent.mkdir(parents=True,exist_ok=True)
with path.open("wb") as out:plistlib.dump(plist,out)
subprocess.run([python,str(root/"proxy.py"),"configure"],check=True)
domain="gui/"+str(os.getuid())
loaded=subprocess.run(["launchctl","print",domain+"/"+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
if loaded:subprocess.run(["launchctl","kickstart","-k",domain+"/"+label],check=True)
else:subprocess.run(["launchctl","bootstrap",domain,str(path)],check=True)
print("Installed "+label)

# Add Git tools to the existing coding facade without replacing its terminal tools.
coding=pathlib.Path.home()/".local/share/biome-lmstudio/coding_mcp.py"
if coding.exists():
 content=coding.read_text()
 if "from agent_tools import" not in content:
  marker="def dispatch(message):"
  original="{'start_command': start_command, 'command_status': command_status}"
  replacement="{'start_command': start_command, 'command_status': command_status, 'git_tools': git_tools, 'git_start': git_start, 'git_status': git_status}"
  if marker not in content or original not in content:raise RuntimeError("Coding facade changed; preserve source and integrate Git dispatch explicitly")
  backup=coding.with_name("coding_mcp.pre-gitlab."+__import__('hashlib').sha256(content.encode()).hexdigest()[:12]+".py")
  if not backup.exists():backup.write_text(content)
  imports=chr(10).join(["sys.path.insert(0, "+repr(str(root))+")","from agent_tools import TOOLS as GIT_TOOLS, git_tools, git_start, git_status","TOOLS.extend(GIT_TOOLS)","",""])
  content=content.replace(marker,imports+marker).replace(original,replacement)
  compile(content,str(coding),"exec");coding.write_text(content)
  print("Added Git tools to coding facade")

http=pathlib.Path.home()/".local/share/biome-lmstudio/coding_http.mjs"
if http.exists():
 content=http.read_text()
 if "server.registerTool('git_tools'" not in content:
  needle="  return server;"
  if needle not in content:raise RuntimeError("Coding HTTP facade changed")
  additions="""  server.registerTool('git_tools', {
    description: 'Discover GitLab MCP tools and native Git capabilities through Biome.',
    inputSchema: z.object({})
  }, args => terminal('git_tools', args));
  server.registerTool('git_start', {
    description: 'Start a Git capability through Biome; retain operation and call git_status until settled. Inspect result.ok.',
    inputSchema: z.object({ contract: z.string(), arguments: z.record(z.string(), z.unknown()) })
  }, args => terminal('git_start', args));
  server.registerTool('git_status', {
    description: 'Read a durable Git operation through Biome; queued or uncertain is not success.',
    inputSchema: z.object({ operation: z.string() })
  }, args => terminal('git_status', args));
"""
  backup=http.with_name("coding_http.pre-gitlab."+__import__('hashlib').sha256(content.encode()).hexdigest()[:12]+".mjs")
  if not backup.exists():backup.write_text(content)
  http.write_text(content.replace(needle,additions+needle))
  subprocess.run([shutil.which("node") or "/usr/local/bin/node","--check",str(http)],check=True)
  subprocess.run(["launchctl","kickstart","-k","gui/"+str(os.getuid())+"/org.biomerte.lmstudio-coding-mcp"],check=True)
  print("Published Git tools on coding MCP HTTP facade")
