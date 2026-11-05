"""Additive transform worker; leaves the existing agent process running."""
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.request import Request,urlopen
from ..provider import gateway
from .capabilities import offers,adapter,bind_contract

ROOT=Path.home()/'.local/share/biome-field-transform'
FIELD_ROOT=Path.home()/'.local/share/biome-activation-field'


def api(path,body=None):
    token=(FIELD_ROOT/'api-token').read_text().strip()
    request=Request('http://127.0.0.1:8793'+path,None if body is None else json.dumps(body).encode(),
                    {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urlopen(request) as response:return json.load(response)


def configure():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    g=gateway();identity=ROOT/'identity.json'
    if not identity.exists():g.save_json(identity,dict(node=hashlib.sha256(os.urandom(32)).hexdigest()))
    node=json.loads(identity.read_text())['node'];site=g.invoke('system.info')['site']
    resource='model.field-transform.mac.'+node[:20]
    cfg=dict(site=site,realm='site',parallelism=1,timeout=None,poll_seconds=2,retry_seconds=5,
             offers=offers(g,resource,'activation_field.transforms.provider'))
    g.save_json(ROOT/'provider.json',cfg)
    return dict(provider=node,resource=resource,contracts=[o['contract'] for o in cfg['offers']])


def main():
    mode=sys.argv[1]
    if mode=='configure':print(json.dumps(configure()));return
    if mode=='adapter':
        try:
            result=adapter(bind_contract(json.load(sys.stdin),sys.argv[2] if len(sys.argv)>2 else None),api)
            if result is None:raise ValueError('Transform operation route required')
        except Exception as exc:result=dict(ok=False,error=dict(code=type(exc).__name__,message=str(exc)[:300]))
        print(json.dumps(dict(result=result)));return
    if mode!='worker':raise ValueError(mode)
    sys.path.insert(0,str(Path.home()/'.local/share/biome-workspaces/runtime'))
    from biomerte.worker import run_worker
    cfg=json.loads((ROOT/'provider.json').read_text());g=gateway()
    class Remote:
        site=cfg['site'];realm='site';node=json.loads((ROOT/'identity.json').read_text())['node']
        def call(self,name,args,rid=None):return g.invoke(name,args,rid,self.realm)
    run_worker(str(ROOT),Remote(),cfg,False)


if __name__=='__main__':main()
