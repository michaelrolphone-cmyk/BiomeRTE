"""Install only this package's native launch agents; no competing services edited."""
import json,os,plistlib,subprocess,sys
from pathlib import Path
from activation_field.provider import configure

root=Path.home()/'.local/share/biome-activation-field'
source=Path(__file__).resolve().parent
domain='gui/'+str(os.getuid())
for name,argv in [('activation-field-api',[sys.executable,'-m','activation_field.cli','serve']),('activation-field-agent',[sys.executable,'-m','activation_field.provider','worker'])]:
    label='org.biomerte.'+name;path=Path.home()/'Library/LaunchAgents'/(label+'.plist')
    spec={'Label':label,'ProgramArguments':argv,'WorkingDirectory':str(source),'EnvironmentVariables':{'PATH':str(root/'.venv/bin')+':/usr/bin:/bin','PYTHONPATH':str(source)},'RunAtLoad':True,'KeepAlive':True,'StandardOutPath':str(root/(name+'.log')),'StandardErrorPath':str(root/(name+'-error.log'))}
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('wb') as f:plistlib.dump(spec,f)
    if name.endswith('agent'):print(json.dumps(configure()))
    loaded=subprocess.run(['launchctl','print',domain+'/'+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    subprocess.run(['launchctl','kickstart','-k',domain+'/'+label] if loaded else ['launchctl','bootstrap',domain,str(path)],check=True)
