import hashlib,json,platform,subprocess
from pathlib import Path


def digest_file(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()


def read_manifest(path,verify=True):
    m=json.loads(Path(path).read_text())
    if m['schema_version']!=1 or not m['revision'] or not m['baseline_hash']:
        raise ValueError("Unpinned model")
    c=m['config'];text=c.get('text_config',c)
    q=c.get('quantization',c.get('quantization_config',{}))
    if text['num_hidden_layers']!=32 or q.get('bits')!=4 or q.get('group_size')!=64:
        raise ValueError("Expected complete affine4/group64 32-layer Qwen3.5")
    if text.get('num_experts',0):raise ValueError("Dense model required")
    if verify:
        for name,desc in m['files'].items():
            f=Path(m['path'])/name
            if f.stat().st_size!=desc['bytes'] or digest_file(f)!=desc['sha256']:
                raise ValueError("Model hash mismatch: "+name)
    return m


def hardware():
    if platform.system()!='Darwin' or platform.machine()!='arm64':raise RuntimeError("Native Apple Silicon Mac required")
    return dict(system=platform.platform(),machine=platform.machine(),memory_bytes=int(subprocess.check_output(['sysctl','-n','hw.memsize'])))
