"""Prove baseline-only AFWT restoration in a macOS filesystem sandbox.

The parent compares hashes only after the isolated child has exited. No target
checkpoint or fitting observation can be opened by the materializer process.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def save(path,value):
    from activation_field.transforms.common import atomic_bytes,canonical
    atomic_bytes(path,canonical(value))


def child(args):
    from activation_field.checkpoint import Vault
    from activation_field.config import FieldConfig
    from activation_field.manifest import read_manifest
    from activation_field.runtime import Engine
    from activation_field.transforms.source import RuntimeSource
    from activation_field.transforms.materialize import materialize
    # The unsandboxed parent owns the normal FIFO slot for both subprocess
    # restoration and later diagnostics. macOS disallows setuid ps in a sandbox,
    # so process admission must be acquired before entering that sandbox.
    if args.admission_fd is None:raise RuntimeError('Parent-owned model admission required')
    with os.fdopen(args.admission_fd) as permit:owner=json.load(permit)
    if owner['pid']!=os.getppid():raise RuntimeError('Admission parent changed')
    runtime=Path.home()/'.local/share/biome-model-runtime';sys.path.insert(0,str(runtime))
    from model_runtime import ModelRuntime
    queue=ModelRuntime().state()
    if not queue or queue[0]['pid']!=owner['pid'] or queue[0]['phase']!='running':
        raise RuntimeError('Parent does not own running model admission')
    import fcntl
    with (runtime/'active.lock').open('a+b') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:pass
        else:raise RuntimeError('Parent admission lock is not held')
    denied=[]
    for name in args.forbid_file:
        try:Path(name).read_bytes()
        except PermissionError:denied.append(name)
        else:raise AssertionError('Required source isolation did not deny '+name)
    report=dict(status='running',source_reads_denied=denied,source_access='macOS sandbox deny file-read*')
    destination=Path(args.output);destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    last=[None]
    def progress(value):
        stamp=(value.get('stage'),value.get('tensor'),value.get('waiting_for'))
        if stamp!=last[0]:
            report.update(progress=value,updated_unix=time.time());save(destination/'progress.json',report);last[0]=stamp
    engine=source=None
    started=time.monotonic()
    try:
        engine=Engine(read_manifest(args.manifest),FieldConfig.parse(dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False)))
        vault=Vault(destination/'state');source=RuntimeSource(engine,vault)
        result=materialize(source,args.artifact,vault,args.checkpoint,destination/'staging',progress=progress)
        report.update(status='succeeded',result=result,source_revision=engine.manifest['source_revision'],seconds=time.monotonic()-started)
    finally:
        source=None
        if engine:engine.close()
    save(destination/'child-result.json',report)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',required=True);p.add_argument('--artifact',required=True)
    p.add_argument('--forbid-dir',action='append',required=True);p.add_argument('--forbid-file',action='append',required=True)
    p.add_argument('--comparison-vault',required=True);p.add_argument('--checkpoint',required=True)
    p.add_argument('--target-checkpoint');p.add_argument('--fit-config')
    p.add_argument('--output',required=True);p.add_argument('--child',action='store_true')
    p.add_argument('--admission-fd',type=int)
    args=p.parse_args()
    if args.child:return child(args)
    sandbox=Path('/usr/bin/sandbox-exec')
    if not sandbox.is_file():raise RuntimeError('macOS sandbox-exec unavailable; portability is unverified')
    destination=Path(args.output);destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in args.forbid_file:
        if not Path(name).is_file():raise FileNotFoundError('Expected source file absent before sandbox: '+name)
    profile='(version 1) (allow default) (deny file-read* '+''.join('(subpath '+json.dumps(str(Path(n).resolve()))+')' for n in args.forbid_dir)+')'
    profile_path=destination/'deny-source.sb';profile_path.write_text(profile);profile_path.chmod(0o600)
    save(destination/'progress.json',dict(status='starting',source_files_verified_present=args.forbid_file,updated_unix=time.time()))
    from activation_field.admission import model_slot
    def progress(value):save(destination/'progress.json',dict(status='running',phase='model_admission',progress=value,updated_unix=time.time()))
    with model_slot(progress):return admitted(args,destination,profile_path)


def admitted(args,destination,profile_path):
    read_fd,write_fd=os.pipe()
    try:
        with os.fdopen(write_fd,'w') as permit:json.dump(dict(pid=os.getpid()),permit)
        with (destination/'child.log').open('ab') as log:
            os.chmod(log.name,0o600)
            completed=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile_path),sys.executable,str(Path(__file__).resolve()),*sys.argv[1:],'--child','--admission-fd',str(read_fd)],pass_fds=(read_fd,),stdout=log,stderr=log)
    finally:os.close(read_fd)
    if completed.returncode:
        save(destination/'result.json',dict(status='failed',child_exit=completed.returncode,log=str(destination/'child.log')))
        raise RuntimeError('Isolated materialization failed; see '+str(destination/'child.log'))
    from activation_field.checkpoint import Vault
    from activation_field.transforms.common import digest
    actual=Vault(destination/'state').manifest(args.checkpoint)
    expected=Vault(args.comparison_vault).manifest(args.checkpoint)
    def tensor_hashes(m):
        return {t['name']:dict(shape=t['shape'],dtype=t['dtype'],chunks=[{k:c[k] for k in ('start','rows','sha256')} for c in t['chunks']]) for t in m['tensors']}
    assert tensor_hashes(actual)==tensor_hashes(expected),'Restored packed tensors differ'
    for name in ('baseline_hash','source_transform','source_transform_bytes','native_tokens','consumed_tokens'):
        assert actual['metadata'][name]==expected['metadata'][name],name
    assert actual['metadata']['config']['read_only'] and not actual['metadata']['config']['enabled']
    assert actual['metadata']['config']['baseline_pull']==0
    report=json.loads((destination/'child-result.json').read_text())
    report.update(comparison='all plaintext tensor/chunk hashes equal after isolated child exited',
                  matching_tensor_records=len(actual['tensors']),tensor_digest=digest(tensor_hashes(actual)),
                  original_checkpoint_manifest_sha256=digest(expected),isolated_checkpoint_manifest_sha256=digest(actual))
    if args.target_checkpoint:
        # This separate parent phase may read the target. It never supplies data
        # to the already-completed, sandboxed baseline-only materializer.
        from activation_field.config import FieldConfig
        from activation_field.manifest import read_manifest
        from activation_field.runtime import Engine
        from activation_field.transforms.source import RuntimeSource
        from activation_field.transforms.format import load_transform
        from activation_field.transforms.materialize import validate_baseline
        from activation_field.transforms.evaluate import numerical_scan
        from activation_field.transforms.fit import configuration
        transform=load_transform(args.artifact)
        config=configuration(json.loads(Path(args.fit_config).read_text()) if args.fit_config else {})
        assert digest(config)==transform[0]['provenance']['configuration_digest'],'Use the recorded fit configuration for diagnostic splits'
        def progress(value):save(destination/'progress.json',dict(status='running',phase='full_numerical_scan',progress=value,updated_unix=time.time()))
        engine=source=None;started=time.monotonic()
        try:
            engine=Engine(read_manifest(args.manifest),FieldConfig.parse(dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False)))
            source=RuntimeSource(engine,Vault(args.comparison_vault),args.target_checkpoint)
            validate_baseline(source,transform[0])
            assert source.identity['target_hash']==transform[0]['provenance']['target_checkpoint_hash'],'Target provenance mismatch'
            numerical=numerical_scan(source,transform,config,progress)
            numerical.update(candidate_sha256=report['result']['artifact']['sha256'],seconds=time.monotonic()-started,
                             candidate_selection='saved partial candidate selected before final diagnostics; full fit continues separately')
            report['numerical']=numerical
        finally:
            source=None
            if engine:engine.close()
    save(destination/'result.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('result','progress','numerical')}))
    if report.get('numerical'):print(json.dumps(dict(global_metrics=report['numerical']['global_metrics'])))


if __name__=='__main__':main()
