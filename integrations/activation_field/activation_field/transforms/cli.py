import argparse
import json
import shutil
from pathlib import Path
from ..checkpoint import Vault
from .fit import configuration


def main(argv=None):
    root=Path.home()/'.local/share/biome-activation-field'
    parser=argparse.ArgumentParser(prog='fieldctl transform');sub=parser.add_subparsers(dest='action',required=True)
    fit=sub.add_parser('fit');fit.add_argument('--base',required=True);fit.add_argument('--target',required=True);fit.add_argument('--config',required=True);fit.add_argument('--output',required=True);fit.add_argument('--resume',action='store_true');fit.add_argument('--vault',default=str(root/'state'))
    candidates=sub.add_parser('candidates');candidates.add_argument('--run',required=True)
    export=sub.add_parser('export');export.add_argument('--run',required=True);export.add_argument('--candidate',required=True);export.add_argument('--output',required=True)
    inspect=sub.add_parser('inspect');inspect.add_argument('artifact')
    materialize=sub.add_parser('materialize');materialize.add_argument('--base',required=True);materialize.add_argument('--transform',required=True);materialize.add_argument('--output',required=True);materialize.add_argument('--vault',default=str(root/'state'));materialize.add_argument('--resume',action='store_true')
    compare=sub.add_parser('compare');compare.add_argument('--base',required=True);compare.add_argument('--target',required=True);compare.add_argument('--restored',required=True);compare.add_argument('--suite',required=True);compare.add_argument('--output',required=True);compare.add_argument('--vault',default=str(root/'state'));compare.add_argument('--resume',action='store_true')
    args=parser.parse_args(argv)
    if args.action=='inspect':
        from .format import inspect_transform
        result=inspect_transform(args.artifact)
    elif args.action=='candidates':
        result=Vault(Path(args.run)/'private').load_json('fit.enc')['candidates']
    elif args.action=='export':
        from .jobs import identifier
        from .common import atomic_bytes
        from .format import inspect_transform, read_artifact
        candidate=identifier(args.candidate);source=Path(args.run)/(candidate+'.afwt.enc')
        result=inspect_transform(source);atomic_bytes(args.output,read_artifact(source))
    else:
        from ..manifest import read_manifest
        from ..admission import model_slot
        from ..runtime import Engine
        from ..config import FieldConfig
        from .source import RuntimeSource
        engine=None;source=None
        with model_slot():
            try:
                engine=Engine(read_manifest(args.base),FieldConfig.parse(dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False)))
                vault=Vault(args.vault)
                if args.action=='fit':
                    from .fit import fit
                    c=configuration(json.loads(Path(args.config).read_text()))
                    source=RuntimeSource(engine,vault,args.target,tile_bytes=c['tile_bytes'])
                    result=fit(source,args.output,c,args.resume)
                elif args.action=='materialize':
                    from .materialize import materialize
                    checkpoint=Path(args.output).name
                    result=materialize(RuntimeSource(engine,vault),args.transform,vault,checkpoint,Path(args.output).parent/(checkpoint+'.pending'),args.resume)
                else:
                    from .compare import compare
                    result=compare(engine,vault,args.target,args.restored,json.loads(Path(args.suite).read_text()),args.output,args.resume)
            finally:
                source=None
                if engine:engine.close()
    print(json.dumps(result,indent=2,allow_nan=False))


def session_import(argv):
    import uuid
    from ..provider import api
    parser=argparse.ArgumentParser(prog='fieldctl session import')
    parser.add_argument('--checkpoint',required=True);parser.add_argument('--new-session',action='store_true',required=True);parser.add_argument('--read-only',action='store_true',required=True)
    args=parser.parse_args(argv)
    result=api('/sessions/import',dict(checkpoint_id=args.checkpoint,request_id='fieldctl-import-'+uuid.uuid4().hex,expected_version=0,persist=True,reset_native_state=True,config=dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False)))
    print(json.dumps(result,indent=2))
