import argparse,json,os
from pathlib import Path


def main():
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='transform':
        from .transforms.cli import main as transform_main
        return transform_main(sys.argv[2:])
    if sys.argv[1:3]==['session','import']:
        from .transforms.cli import session_import
        return session_import(sys.argv[3:])
    root=Path.home()/'.local/share/biome-activation-field'
    parser=argparse.ArgumentParser(prog='fieldctl');sub=parser.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve');serve.add_argument('--root',default=str(root));serve.add_argument('--manifest',default=str(root/'model-manifest.json'));serve.add_argument('--port',type=int,default=8791);serve.add_argument('--config')
    model=sub.add_parser('model');ms=model.add_subparsers(dest='operation',required=True)
    inspect=ms.add_parser('inspect');inspect.add_argument('--manifest',default=str(root/'model-manifest.json'))
    prepare=ms.add_parser('prepare');prepare.add_argument('--source',required=True);prepare.add_argument('--revision',required=True);prepare.add_argument('--bits',type=int,default=4);prepare.add_argument('--group-size',type=int,default=64);prepare.add_argument('--output',default=str(root/'model-manifest.json'))
    ex=sub.add_parser('experiment');es=ex.add_subparsers(dest='operation',required=True)
    run=es.add_parser('run');run.add_argument('--config',required=True);run.add_argument('--resume',action='store_true')
    report=es.add_parser('report');report.add_argument('--run',required=True);report.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.command=='serve':
        import uvicorn
        from .server import app
        from .config import FieldConfig
        initial=FieldConfig.parse(json.loads(Path(args.config).read_text()) if args.config else {}).to_dict()
        uvicorn.run(app(args.root,args.manifest,initial),host='127.0.0.1',port=args.port,access_log=False,log_level='warning')
    elif args.command=='model' and args.operation=='inspect':
        from .manifest import read_manifest
        m=read_manifest(args.manifest);print(json.dumps({k:v for k,v in m.items() if k!='files'},indent=2))
    elif args.command=='model':
        from huggingface_hub import snapshot_download,HfApi
        from .manifest import digest_file,read_manifest
        import hashlib,importlib.metadata
        if args.bits!=4 or args.group_size!=64:raise ValueError('First experiment requires affine4/group64')
        revision=HfApi().model_info(args.source,revision=args.revision).sha
        folder=Path(snapshot_download(args.source,revision=revision,allow_patterns=['*.json','*.safetensors','*.jinja','*.txt','*.model']))
        files={f.name:dict(sha256=digest_file(f),bytes=f.stat().st_size) for f in folder.iterdir() if f.is_file()}
        manifest=dict(schema_version=1,repository=args.source,revision=revision,path=str(folder),files=files,config=json.loads((folder/'config.json').read_text()),versions={d.metadata['Name']:d.version for d in importlib.metadata.distributions()},baseline_hash=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest())
        Path(args.output).write_text(json.dumps(manifest,indent=2));read_manifest(args.output)
    elif args.operation=='run':
        from experiments.evaluate import run
        print(run(json.loads(Path(args.config).read_text()),args.resume))
    else:
        config=json.loads((Path(args.run)/'config.json').read_text())
        kind=config.get('kind','memory')
        if kind=='memory':
            from experiments.evaluate import report
            result=report(args.run,args.output)
        else:
            filename='stability.json' if kind=='stability' else 'descriptor-report.json'
            result=json.loads((Path(args.run)/filename).read_text())
            from experiments.storage import atomic_json
            atomic_json(Path(args.output)/filename,result)
        print(json.dumps(result,indent=2))

if __name__=='__main__':main()
