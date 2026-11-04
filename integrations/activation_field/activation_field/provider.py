"""Durable Biome provider: normal agent request/context contract, local engine."""
import json,os,sys,uuid
from pathlib import Path
from urllib.request import Request,urlopen

ROOT=Path.home()/'.local/share/biome-activation-field'


def gateway():
    sys.path.insert(0,str(Path.home()/'.local/share/biome-mac-terminal'))
    import mac_provider
    return mac_provider


def api(path,body=None,method=None):
    req=Request('http://127.0.0.1:8791'+path,None if body is None else json.dumps(body).encode(),{'Authorization':'Bearer '+(ROOT/'api-token').read_text().strip(),'Content-Type':'application/json'},method=method)
    with urlopen(req) as response:return json.load(response)


def configure():
    import hashlib
    g=gateway();nodefile=ROOT/'identity.json'
    if not nodefile.exists():g.save_json(nodefile,dict(node=hashlib.sha256(os.urandom(32)).hexdigest()))
    node=json.loads(nodefile.read_text())['node'];site=g.invoke('system.info')['site']
    name='agent.activation_field.delegate';contract=name+':1';resource='agent.activation-field.mac.'+node[:20]
    usage=dict(before='Experimental full Qwen3.5 9B. Native Mac engine; shared model/image FIFO. Context config.enabled=true opts into weight writes. baseline_pull defaults to zero. This normal agent returns plaintext results through Biome; use synthetic/nonprivate test prompts here.',parameters={'request':'Text prompt for generation; ignored for management actions.','context':'action=generate|health|diagnostics|config|reset|checkpoint|fork|delete|import; session_id and expected_version required for existing sessions; config, messages, max_tokens, temperature and kind as appropriate. New sessions default field disabled. Reuse returned session_id and version. Full transcript messages must include the exact previous assistant response; otherwise supply request as a new turn through the adapter.'},example_inputs={'request':'Remember that my test marker is jade.','context':{'config':{'enabled':True,'baseline_pull':0},'max_tokens':32}},read_result='Read result.ok, result.session_id, result.version, result.response, and result.diagnostics. No scientific memory success is claimed.')
    g.invoke('cap.define',dict(name=name,version=1,inputs={'request':'str','context':'obj'},outputs={'result':'obj'},description='Activation Field — Qwen3.5 9B: experimental writable quantized full-model agent.',usage=usage),rid='af-capability-definition-v1')
    cfg=dict(site=site,realm='site',parallelism=1,timeout=None,poll_seconds=2,retry_seconds=5,offers=[dict(contract=contract,resource=resource,service='activation-field',capacity=1,usage=usage,argv=[sys.executable,'-m','activation_field.provider','adapter'])])
    g.save_json(ROOT/'provider.json',cfg)
    return dict(provider=node,resource=resource,contract=contract)


def adapter(payload):
    context=payload['inputs']['context'];request=payload['inputs']['request'];action=context.get('action','generate');rid=payload['operation']
    if action=='health':return dict(ok=True,**api('/health'))
    if action=='import':
        body={k:v for k,v in context.items() if k in ('checkpoint_id','config','persist','reset_native_state')}
        body.update(request_id=rid,expected_version=0)
        return dict(ok=True,**api('/sessions/import',body))
    sid=context.get('session_id')
    if not sid:
        created=api('/sessions',dict(request_id=rid+'-create',expected_version=0,config=context.get('config',{}),persist=context.get('persist',False)))
        sid=created['session_id'];version=created['version']
    else:
        state=api('/sessions/'+sid);version=context.get('expected_version')
        if version is None:raise ValueError('Existing sessions require expected_version')
    if action=='diagnostics':return dict(ok=True,session_id=sid,diagnostics=api('/sessions/'+sid+'/diagnostics'))
    body=dict(request_id=rid,expected_version=version)
    for key in ['config','kind','field_only','retain_traces','reset_native_state']: 
        if key in context:body[key]=context[key]
    if action=='generate':
        if 'messages' in context:messages=context['messages']
        else:
            # The service records transcript hashes, not plaintext history. For
            # append-only single-prompt calls we reset ONLY ordinary state, so
            # retained context is explicitly weight memory, not hidden replay.
            if context.get('session_id'):
                reset=api('/sessions/'+sid+'/reset',dict(request_id=rid+'-ordinary-reset',expected_version=version,kind='ordinary_state'))
                if reset['status']!='succeeded':return dict(ok=False,receipt=reset)
                body['expected_version']=reset['version']
            messages=[{'role':'user','content':request}]
        body.update(messages=messages,max_tokens=context.get('max_tokens',128),temperature=context.get('temperature',0.),stream=False)
    method='PATCH' if action=='config' else 'DELETE' if action=='delete' else 'POST'
    path='/sessions/'+sid+('' if action=='delete' else '/'+action)
    result=api(path,body,method)
    return dict(ok=result['status']=='succeeded',session_id=sid,version=result['version'],response=result.get('result'),status=result['status'])


def main():
    mode=sys.argv[1]
    if mode=='configure':print(json.dumps(configure()));return
    if mode=='adapter':
        try:result=adapter(json.load(sys.stdin))
        except Exception as exc:result=dict(ok=False,error=dict(code=type(exc).__name__,message=str(exc)[:300]))
        print(json.dumps(dict(result=result)));return
    if mode=='worker':
        sys.path.insert(0,str(Path.home()/'.local/share/biome-workspaces/runtime'))
        from biomerte.worker import run_worker
        cfg=json.loads((ROOT/'provider.json').read_text());g=gateway()
        class Remote:
            site=cfg['site'];realm='site';node=json.loads((ROOT/'identity.json').read_text())['node']
            def call(self,name,args,rid=None):return g.invoke(name,args,rid,self.realm)
        run_worker(str(ROOT),Remote(),cfg,False)

if __name__=='__main__':main()
