#!/usr/bin/env python3
"""Install tested model admission at idle boundaries, retaining existing jobs."""
from pathlib import Path
import base64
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

HOME=Path.home()
STAGE=Path(__file__).resolve().parent
ROLLOUT=STAGE.parent
STATE=HOME/'.local/share/biome-model-runtime'
sys.path.insert(0,str(HOME/'.local/share/biome-mac-terminal'))
import mac_provider as gateway
HTTP=build_opener(ProxyHandler({}))
GUI='gui/'+str(os.getuid())+'/'
LEASES=[]
SERVICES=['org.biomerte.comfyui','org.biomerte.private-image','org.biomerte.private-chat','org.biomerte.agent.lmstudio']

def status(phase,**detail):
    value=dict(phase=phase,**detail);temp=ROLLOUT/'deployment.next';temp.write_text(json.dumps(value));temp.replace(ROLLOUT/'deployment.json');print(json.dumps(value),flush=True)

def service(label):return subprocess.run(['launchctl','print',GUI+label],capture_output=True,text=True)
def signal(label,name):return subprocess.run(['launchctl','kill',name,GUI+label],capture_output=True,text=True)
def restart(label):
    if service(label).returncode:
        subprocess.run(['launchctl','bootstrap',GUI[:-1],str(HOME/'Library/LaunchAgents'/(label+'.plist'))],check=True)
    subprocess.run(['launchctl','kickstart','-k',GUI+label],check=True)

def atomic(target,content):
    target.parent.mkdir(parents=True,exist_ok=True);temp=target.with_name(target.name+'.model-next');temp.write_bytes(content);os.chmod(temp,0o600);temp.replace(target)

def image_queue():return json.load(HTTP.open('http://127.0.0.1:8188/queue'))
def unsettled():
    with sqlite3.connect(HOME/'.local/share/biome-lmstudio/agent.sqlite') as db:
        return db.execute('SELECT count(*) FROM work WHERE outcome IS NULL').fetchone()[0]

def acquire_maintenance():
    chat=json.loads((HOME/'.local/share/biome-private-chat/manifest.json').read_text())['resource']
    specs=[('image.generate:1',chat.replace('privatechat.mac.','image.mac.',1)),('chat.private.exchange:2',chat)]
    LEASES.extend([None,None])
    while True:
        states=[]
        for i,(contract,resource) in enumerate(specs):
            state=gateway.invoke('cap.lease',{'lease':LEASES[i]})['state'] if LEASES[i] else 'missing'
            if state not in ('bound','queued','fencing'):
                acquired=gateway.invoke('cap.acquire',{'contract':contract,'resource':resource,'task':'model-runtime-install-'+str(os.getpid())})
                LEASES[i]=acquired['id'];state=acquired['state']
            states.append(state)
        if all(state=='bound' for state in states):return
        time.sleep(1)

def main():
    expected=json.loads((STAGE/'baseline-sha256.json').read_text())
    destinations={
      'private_chat.py':HOME/'.local/share/biome-lowlat-build/providers/private-chat/private_chat.py',
      'comfy_backend.py':HOME/'.local/share/biome-lowlat-build/providers/private-image/comfy_backend.py',
      'lmstudio_agent.py':HOME/'.local/share/biome-lmstudio/lmstudio_agent.py',
      'memory_node.py':HOME/'Developer/biome-comfyui/ComfyUI/custom_nodes/biome_private_image/__init__.py'}
    backups={}
    paused=False;paused_providers=[];updated=False;complete=False
    try:
        for name,target in destinations.items():
            original=target.read_bytes()
            if hashlib.sha256(original).hexdigest()!=expected[name]:raise RuntimeError('Source changed: '+name)
            backups[target]=original
        status('waiting_for_maintenance')
        acquire_maintenance()
        while True:
            result=service('org.biomerte.agent.lmstudio')
            running=result.returncode==0 and re.search(r'\bpid = \d+',result.stdout)
            if running:
                if signal('org.biomerte.agent.lmstudio','SIGSTOP').returncode:continue
                paused=True
            # Stop intake first, then observe the durable delegation table. An
            # in-flight job is resumed and allowed to finish, never killed.
            if unsettled()==0:break
            if paused:signal('org.biomerte.agent.lmstudio','SIGCONT');paused=False
            status('waiting_for_coding_job');time.sleep(1)
        while True:
            q=image_queue()
            if not q['queue_running'] and not q['queue_pending']:break
            status('waiting_for_existing_image');time.sleep(1)
        for label in ['org.biomerte.private-image','org.biomerte.private-chat']:
            if signal(label,'SIGSTOP').returncode==0:paused_providers.append(label)
        status('installing_at_idle')
        STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
        atomic(STATE/'model_runtime.py',(STAGE/'model_runtime.py').read_bytes())
        cfg=json.loads((HOME/'.local/share/biome-lmstudio/coding-config.json').read_text())
        atomic(STATE/'config.json',json.dumps({'default_llm_model':'qwen3.5-9b','default_load':{'context_length':cfg['context_length']}}).encode())
        updated=True
        for name,target in destinations.items():atomic(target,(STAGE/name).read_bytes())
        restart('org.biomerte.comfyui')
        while True:
            try:
                state=json.load(HTTP.open('http://127.0.0.1:8188/biome/model-memory'))
                if state['running']==0 and state['loaded_models']==0:break
            except HTTPError:
                raise RuntimeError('ComfyUI memory release API did not load') from None
            except URLError:
                pass
            time.sleep(.5)
        sys.path.insert(0,str(STATE));from model_runtime import ModelRuntime
        runtime=ModelRuntime()
        # At this fenced idle boundary, remove legacy simultaneous residents.
        # Subsequent chat requests retain their chosen model until another job.
        with runtime.slot('image'):pass
        runtime.warm_default()
        restart('org.biomerte.private-image');restart('org.biomerte.private-chat');restart('org.biomerte.agent.lmstudio');paused=False;paused_providers=[]
        launch=HOME/'Library/LaunchAgents/org.biomerte.model-runtime.plist'
        label='org.biomerte.model-runtime'
        plist={'Label':label,'ProgramArguments':[str(HOME/'miniconda3/bin/python3'),'-S','-u',str(STATE/'model_runtime.py')],
               'RunAtLoad':True,'KeepAlive':True,'StandardOutPath':str(STATE/'runtime.log'),'StandardErrorPath':str(STATE/'runtime.log'),
               'WorkingDirectory':str(STATE),'Umask':0o077}
        atomic(launch,plistlib.dumps(plist));restart(label)
        snapshot={'memory':runtime.models.comfy('/biome/model-memory'),
                  'loaded_llms':[i['id'] for m in runtime.models.models() for i in m.get('loaded_instances',[])],
                  'queue':runtime.state()}
        complete=True;status('applied',**snapshot)
    except Exception as exc:
        if updated:
            for target,content in backups.items():atomic(target,content)
            for label in SERVICES:
                try:restart(label)
                except Exception:pass
            paused=False
        status('failed',error=type(exc).__name__,reason=str(exc)[:180]);raise
    finally:
        if paused:signal('org.biomerte.agent.lmstudio','SIGCONT')
        for label in paused_providers:signal(label,'SIGCONT')
        for lease in LEASES:
            if lease is None:continue
            try:gateway.invoke('cap.release',{'lease':lease})
            except Exception:pass

if __name__=='__main__':main()
