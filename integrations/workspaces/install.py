#!/usr/bin/env python3
import json,os,pathlib,plistlib,shutil,subprocess,sys
src=pathlib.Path(__file__).resolve().parent;root=pathlib.Path.home()/'.local/share/biome-workspaces';root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
for name in ('manager.py','provider.py','schemas.json'):shutil.copy2(src/name,root/name)
shutil.copy2(src/'agent_tools.py',root/'workspace_agent_tools.py')
shutil.copytree(src.parent.parent/'biomerte',root/'runtime/biomerte',dirs_exist_ok=True)
sys.path.insert(0,str(root));import manager
for t in json.loads((src/'templates.json').read_text()):manager.template_publish({'template':t})
subprocess.run([sys.executable,str(root/'provider.py'),'configure'],check=True)
domain='gui/'+str(os.getuid())
for mode in ('worker','monitor'):
 label='org.biomerte.workspace-'+mode;path=pathlib.Path.home()/'Library/LaunchAgents'/(label+'.plist')
 plist={'Label':label,'ProgramArguments':[sys.executable,str(root/'provider.py'),mode],'RunAtLoad':True,'KeepAlive':True,'WorkingDirectory':str(root),'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},'StandardOutPath':str(root/(mode+'.log')),'StandardErrorPath':str(root/(mode+'-error.log'))}
 with path.open('wb') as f:plistlib.dump(plist,f)
 loaded=subprocess.run(['launchctl','print',domain+'/'+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
 subprocess.run(['launchctl','kickstart','-k',domain+'/'+label] if loaded else ['launchctl','bootstrap',domain,str(path)],check=True)
coding=pathlib.Path.home()/'.local/share/biome-lmstudio/coding_mcp.py';s=coding.read_text()
if 'workspace_agent_tools' not in s:
 marker='def dispatch(message):';needle="'git_status': git_status}"
 if marker not in s or needle not in s:raise RuntimeError('Coding facade changed; integrate explicitly')
 shutil.copy2(coding,coding.with_name('coding_mcp.pre-workspaces.py'))
 s=s.replace(marker,'sys.path.insert(0, '+repr(str(root))+')\nfrom workspace_agent_tools import TOOLS as WS_TOOLS, workspace_tools, workspace_start, workspace_status\nTOOLS.extend(WS_TOOLS)\n\n'+marker).replace(needle,"'git_status': git_status, 'workspace_tools': workspace_tools, 'workspace_start': workspace_start, 'workspace_status': workspace_status}")
 compile(s,str(coding),'exec');coding.write_text(s)
http=coding.with_name('coding_http.mjs');s=http.read_text()
if "server.registerTool('workspace_tools'" not in s:
 shutil.copy2(http,http.with_name('coding_http.pre-workspaces.mjs'))
 additions="""  server.registerTool('workspace_tools', {description: 'Discover workflow workspace capabilities and schemas.', inputSchema: z.object({})}, args => terminal('workspace_tools', args));
  server.registerTool('workspace_start', {description: 'Start a workspace capability through Biome; retain operation and inspect result.ok.', inputSchema: z.object({contract:z.string(),arguments:z.record(z.string(),z.unknown())})}, args => terminal('workspace_start', args));
  server.registerTool('workspace_status', {description: 'Read durable workspace operation status.', inputSchema: z.object({operation:z.string()})}, args => terminal('workspace_status', args));
"""
 if '  return server;' not in s:raise RuntimeError('Coding HTTP facade changed')
 http.write_text(s.replace('  return server;',additions+'  return server;'))
 subprocess.run([shutil.which('node'),'--check',str(http)],check=True)
 subprocess.run(['launchctl','kickstart','-k',domain+'/org.biomerte.lmstudio-coding-mcp'],check=True)
print('Workspace provider, lifecycle monitor and coding tools installed')
