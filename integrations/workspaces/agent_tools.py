import sys,json,uuid
from pathlib import Path
sys.path.insert(0,str(Path.home()/'.local/share/biome-workspaces'))
import manager as wm
from provider import OPERATIONS
def workspace_tools(args):return {'contracts':['workspace.'+x+':1' for x in OPERATIONS],'schemas':wm.read(wm.ROOT/'schemas.json'),'pattern':'workspace_start({contract,arguments}), then workspace_status({operation}) until settled; inspect result.ok. Pass holder/generation from acquire to all workspace commands.'}
def workspace_start(args):
 if args['contract'] not in ['workspace.'+x+':1' for x in OPERATIONS]:raise ValueError('Unknown workspace contract')
 op='wsop-'+uuid.uuid4().hex
 return wm.gateway().invoke('cap.submit',{'contract':args['contract'],'resource':wm.read(wm.ROOT/'provider.json')['resource'],'operation':op,'inputs':{'arguments':args['arguments']}},op+'-submit')
def workspace_status(args):return wm.gateway().invoke('cap.status',{'operation':args['operation']})
TOOLS=[{'name':'workspace_tools','description':'Discover workflow workspace capabilities and argument schemas.','inputSchema':{'type':'object','properties':{}}},{'name':'workspace_start','description':'Start a durable workspace capability through Biome. Retain operation ID and inspect result.ok.','inputSchema':{'type':'object','properties':{'contract':{'type':'string'},'arguments':{'type':'object'}},'required':['contract','arguments']}},{'name':'workspace_status','description':'Read durable workspace operation result.','inputSchema':{'type':'object','properties':{'operation':{'type':'string'}},'required':['operation']}}]
