"""Pull, verify and retain versioned Pi snapshots in an independently recoverable tar archive."""
import argparse
import datetime
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import quote
from volumes import BASE, VAULT, SACRED, allocate, syncdir, write_json

CHUNK=1024*1024
MAX_HISTORY=24
CONFIG=BASE/'replica.json'
HEALTH=BASE/'replica-health.json'

def db_check(raw, volume):
    db=sqlite3.connect('file:'+quote(str(raw))+'?mode=ro',uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or db.execute('SELECT uuid FROM volume').fetchall()!=[(volume,)]:
            raise ValueError('Failed source SQLite integrity or volume identity check')
        if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Dangling database references')
    finally: db.close()

def extract(archive, entry, target):
    h=hashlib.sha256()
    with gzip.GzipFile(fileobj=archive.extractfile(entry['file'])) as src, target.open('xb') as dst:
        while True:
            b=src.read(CHUNK)
            if not b:break
            dst.write(b);h.update(b)
    if h.hexdigest()!=entry['sha256'] or target.stat().st_size!=entry['size']:raise ValueError('Snapshot checksum mismatch')
    db_check(target,entry['volume'])

def entries(archive):
    try: stream=archive.extractfile('index.json')
    except KeyError:
        if archive.next() is not None or archive.getmembers():raise ValueError('Archive index missing')
        return []
    return json.load(stream)['generations']

def restore(dest, generation=None, vault=VAULT):
    # Link an exclusively created verified file into place. Never overwrite a live database.
    dest=Path(dest).absolute()
    with tarfile.open(vault,'r:') as ar, tempfile.TemporaryDirectory(dir=dest.parent) as td:
        history=entries(ar)
        row=next((g for g in history if generation is None or g['id']==generation),None)
        if row is None:raise ValueError('Generation unavailable')
        raw=Path(td)/'checked.sqlite';extract(ar,row,raw)
        with raw.open('rb') as f:os.fsync(f.fileno())
        os.link(raw,dest);syncdir(dest.parent)
        return row

def add(ar,name,src,size):
    info=tarfile.TarInfo(name);info.size=size;info.mode=0o600
    ar.addfile(info,src)

def padded(n):return 512+((n+511)//512)*512

def publish(raw,meta,old,history,tmp):
    now=time.time()
    gen=str(time.time_ns())+'-'+meta['sha256'][:12]
    gz=tmp/'new.gz'
    with raw.open('rb') as src,gzip.open(gz,'wb',compresslevel=1) as dst:shutil.copyfileobj(src,dst,CHUNK)
    current=dict(id=gen,file='snapshots/'+gen+'.sqlite.gz',time=now,bytes_gz=gz.stat().st_size,
                 **{k:meta[k] for k in ('volume','size','sha256','stamp')})
    kept=[current]
    used=padded(current['bytes_gz'])
    # Retain complete older generations only, newest first, inside the fixed budget.
    for g in history:
        if len(kept)>=MAX_HISTORY:break
        if g['sha256']==current['sha256']:continue
        if used+padded(g['bytes_gz'])>SACRED-256*1024:continue
        kept.append(g);used+=padded(g['bytes_gz'])
    if used>SACRED-256*1024:raise ValueError('Snapshot exceeds the sacred reserve')
    stage=tmp/'verified.tar'
    # APFS clones share unchanged reserved blocks across the atomic replacement.
    # On a filesystem without cloning, a separately allocated stage preserves the old copy.
    if subprocess.run(['/bin/cp','-c',str(VAULT),str(stage)],stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        stage.unlink(missing_ok=True)
        allocate(stage,SACRED)
    with stage.open('r+b') as f:
        with tarfile.open(fileobj=f,mode='w',format=tarfile.USTAR_FORMAT) as ar:
            index=json.dumps({'format':1,'generations':kept},sort_keys=True).encode()
            add(ar,'index.json',io.BytesIO(index),len(index))
            with gz.open('rb') as src:add(ar,current['file'],src,current['bytes_gz'])
            for g in kept[1:]:
                with old.extractfile(g['file']) as src:add(ar,g['file'],src,g['bytes_gz'])
        if f.tell()>SACRED:raise ValueError('Archive exceeds reserve')
        f.flush();os.fsync(f.fileno())
    if stage.stat().st_blocks*512 < SACRED: raise OSError('Staged vault lost its physical reservation')
    raw.unlink()
    with tarfile.open(stage,'r:') as ar:extract(ar,entries(ar)[0],raw)
    os.replace(stage,VAULT);syncdir(BASE)
    return current

def exchange(cfg,old,history,tmp,prior_stamp,host):
    argv=['/usr/bin/ssh','-T','-C','-F','/dev/null','-i',str(BASE/'replica-key'),
          '-o','BatchMode=yes','-o','LogLevel=ERROR','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
          '-o','UserKnownHostsFile='+str(BASE/'known_hosts'),'-o','HostKeyAlias=biome-sacred-pi',
          '-o','ConnectTimeout=8','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','pi@'+host,'snapshot']
    with subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE) as p:
        try:
            p.stdin.write((json.dumps({'stamp':prior_stamp})+'\n').encode());p.stdin.flush()
            meta=json.loads(p.stdout.readline(200000))
            if meta['volume']!=cfg['volume']:raise ValueError('Unexpected Pi volume')
            if meta.get('same'):
                if not history or prior_stamp is None:raise ValueError('Unexpected unchanged response')
                if p.wait(timeout=20):raise IOError('Incomplete response')
                return history[0],prior_stamp,False
            hashes=meta['chunks'];size=meta['size']
            if not 0<size<=SACRED or len(hashes)!=(size+CHUNK-1)//CHUNK:raise ValueError('Invalid snapshot size')
            raw=tmp/'source.sqlite'
            if history:extract(old,history[0],raw)
            else:raw.touch(mode=0o600)
            with raw.open('r+b') as f:
                need=[i for i,h in enumerate(hashes) if hashlib.sha256(f.read(min(CHUNK,size-i*CHUNK))).hexdigest()!=h]
                p.stdin.write((json.dumps(need)+'\n').encode());p.stdin.close()
                for i in need:
                    n=min(CHUNK,size-i*CHUNK);b=p.stdout.read(n)
                    if len(b)!=n or hashlib.sha256(b).hexdigest()!=hashes[i]:raise ValueError('Incomplete or corrupt source block')
                    f.seek(i*CHUNK);f.write(b)
                f.truncate(size)
            if p.stdout.read(1) or p.wait(timeout=30):raise IOError('Incomplete source exchange')
            h=hashlib.sha256()
            with raw.open('rb') as f:
                for b in iter(lambda:f.read(CHUNK),b''):h.update(b)
            if h.hexdigest()!=meta['sha256']:raise ValueError('Source digest mismatch')
            db_check(raw,cfg['volume'])
            if history and history[0]['sha256']==h.hexdigest():return history[0],meta['stamp'],True
            return publish(raw,meta,old,history,tmp),meta['stamp'],True
        finally:
            if p.poll() is None:p.kill()

def sync():
    cfg=json.loads(CONFIG.read_text())
    before=json.loads(HEALTH.read_text()) if HEALTH.exists() else {}
    with (BASE/'replica.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        try:
            for orphan in BASE.glob('.sync-*'):
                if orphan.is_dir() and not orphan.is_symlink():shutil.rmtree(orphan)
            if VAULT.stat().st_size!=SACRED or VAULT.stat().st_blocks*512<SACRED:raise ValueError('Wrong backup reserve')
            with tarfile.open(VAULT,'r:') as old:
                history=entries(old)
                stamp=before.get('stamp') if history and before.get('generation')==history[0]['id'] and time.time()-before.get('verified_at',0)<86400 else None
                last=None
                for host in cfg['hosts']:
                    try:
                        with tempfile.TemporaryDirectory(prefix='.sync-',dir=BASE) as name:
                            row,stamp,verified=exchange(cfg,old,history,Path(name),stamp,host)
                        break
                    except (ConnectionError,EOFError,json.JSONDecodeError,BrokenPipeError) as exc:last=exc
                else:raise RuntimeError('Pi unreachable or snapshot interrupted') from last
            now=time.time()
            write_json(HEALTH,dict(state='current',last_check=now,last_success=now,generation=row['id'],
                       source_sha256=row['sha256'],source_volume=row['volume'],stamp=stamp,
                       verified_at=now if verified else before['verified_at'],interval_seconds=300))
        except Exception as e:
            before.update(state='lagging',last_check=time.time(),error=str(e)[:300])
            write_json(HEALTH,before);raise

if __name__=='__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('action',choices=['sync','restore','list']);p.add_argument('destination',nargs='?');p.add_argument('--generation')
    a=p.parse_args()
    if a.action=='sync':sync()
    elif a.action=='list':
        with tarfile.open(VAULT,'r:') as ar:print(json.dumps(entries(ar),indent=2))
    else: print(json.dumps(restore(a.destination,a.generation)))
