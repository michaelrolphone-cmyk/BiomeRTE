"""Atomic authenticated, chunked checkpoints. No pickle or plaintext spill."""
import hashlib,json,os,secrets,shutil,tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import numpy as np


def canonical(obj):return json.dumps(obj,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


class Vault:
    def __init__(self,root,key=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.root.chmod(0o700)
        p=self.root/'state-key'
        if key is None:
            if not p.exists():
                try:
                    with p.open('xb') as f:os.chmod(p,0o600);f.write(secrets.token_bytes(32));f.flush();os.fsync(f.fileno())
                except FileExistsError:pass
            key=p.read_bytes()
        self.aead=AESGCM(key)

    def seal(self,data,aad):
        nonce=secrets.token_bytes(12)
        return nonce+self.aead.encrypt(nonce,data,aad)

    def open(self,data,aad):return self.aead.decrypt(data[:12],data[12:],aad)

    def save_json(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,tmp=tempfile.mkstemp(dir=p.parent)
        try:
            with os.fdopen(fd,'wb') as f:f.write(self.seal(canonical(value),name.encode()));f.flush();os.fsync(f.fileno())
            os.replace(tmp,p)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)

    def load_json(self,name):return json.loads(self.open((self.root/name).read_bytes(),name.encode()))

    def save(self,name,metadata,tensors):
        if not name or Path(name).name!=name:raise ValueError("Invalid checkpoint ID")
        dest=self.root/name
        if dest.exists():raise FileExistsError(dest)
        temp=Path(tempfile.mkdtemp(prefix='.pending-',dir=self.root));items=[]
        try:
            for index,(key,tensor) in enumerate(tensors):
                # One tensor at a time, chunked to avoid a model-sized shadow.
                shape=list(tensor.shape);dtype=str(np.asarray(tensor[:1] if len(shape) else tensor).dtype)
                chunks=[]
                row_bytes=max(1,int(np.prod(shape[1:]))*np.dtype(dtype).itemsize)
                rows_per_chunk=max(1,4*1024*1024//row_bytes)
                for start in range(0,shape[0] if shape else 1,rows_per_chunk):
                    a=np.asarray(tensor[start:start+rows_per_chunk] if shape else tensor)
                    if a.dtype.kind in 'fc' and not np.isfinite(a).all():raise ValueError("Nonfinite checkpoint tensor")
                    data=a.tobytes();filename=f'{index}-{start}.bin'
                    aad=canonical([name,key,start,shape,dtype])
                    encrypted=self.seal(data,aad)
                    with (temp/filename).open('wb') as f:f.write(encrypted);f.flush();os.fsync(f.fileno())
                    chunks.append(dict(file=filename,start=start,rows=len(a) if shape else 1,sha256=hashlib.sha256(data).hexdigest()))
                items.append(dict(name=key,shape=shape,dtype=dtype,chunks=chunks))
            manifest=dict(schema_version=1,metadata=metadata,tensors=items)
            body=canonical(manifest);fingerprint=hashlib.sha256(body).hexdigest()
            (temp/'manifest.enc').write_bytes(self.seal(body,name.encode()))
            os.replace(temp,dest)
            return dict(checkpoint_id=name,sha256=fingerprint,baseline_hash=metadata['baseline_hash'])
        finally:
            if temp.exists():shutil.rmtree(temp)

    def manifest(self,name):
        if Path(name).name!=name:raise ValueError("Invalid checkpoint ID")
        return json.loads(self.open((self.root/name/'manifest.enc').read_bytes(),name.encode()))

    def arrays(self,name):
        m=self.manifest(name)
        for item in m['tensors']:
            a=np.empty(item['shape'],np.dtype(item['dtype']))
            for c in item['chunks']:
                aad=canonical([name,item['name'],c['start'],item['shape'],item['dtype']])
                data=self.open((self.root/name/c['file']).read_bytes(),aad)
                if hashlib.sha256(data).hexdigest()!=c['sha256']:raise ValueError("Corrupt checkpoint")
                value=np.frombuffer(data,a.dtype)
                if item['shape']:a[c['start']:c['start']+c['rows']]=value.reshape(c['rows'],*item['shape'][1:])
                else:a[...]=value.reshape(())
            yield item['name'],a

    def delete(self,name):
        if Path(name).name!=name:raise ValueError("Invalid checkpoint ID")
        shutil.rmtree(self.root/name,ignore_errors=True)
