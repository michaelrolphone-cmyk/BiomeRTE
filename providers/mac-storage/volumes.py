"""Private, physically reserved Mac storage. Never format a replacement for a missing volume."""
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import tarfile
import tempfile
import uuid

BASE = Path.home() / '.local/share/biome-mac-storage'
MOUNT = BASE / 'workspace.mount'
IMAGE = BASE / 'workspace.dmg'
VAULT = BASE / 'sacred-backups.tar'
GIB = 1024 ** 3
WORKSPACE = 98 * GIB
SACRED = 2 * GIB
MARK = '.biomerte-volume'

def syncdir(p):
    fd = os.open(str(p), os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)

def write_json(p, data):
    with tempfile.NamedTemporaryFile(dir=p.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(json.dumps(data, sort_keys=True).encode())
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp, p)
            syncdir(p.parent)
        finally: tmp.unlink(missing_ok=True)

def allocate(path, size):
    """F_PREALLOCATE followed by ftruncate makes APFS commit real blocks."""
    with path.open('xb') as f:
        result = fcntl.fcntl(f, 42, struct.pack('=Iiqqq', 4, 3, 0, size, 0))
        if struct.unpack('=Iiqqq', result)[-1] < size:
            raise OSError('Incomplete disk reservation')
        f.truncate(size)
        f.flush()
        os.fsync(f.fileno())
    if path.stat().st_blocks * 512 < size:
        raise OSError('Backing file is sparse')

def mounted():
    state = BASE / 'volume.json'
    return (os.path.ismount(MOUNT) and state.is_file() and (MOUNT / MARK).is_file()
            and (MOUNT / MARK).read_text() == json.loads(state.read_text())['workspace_uuid'])

def prepare():
    os.umask(0o077)
    BASE.mkdir(parents=True, exist_ok=True)
    MOUNT.mkdir(mode=0o700, exist_ok=True)
    state = BASE / 'volume.json'
    if state.exists() and (not IMAGE.is_file() or not VAULT.is_file()):
        raise RuntimeError('Enrolled backing file missing; restore before continuing')
    # A staging directory on the same filesystem permits atomic installation.
    if not IMAGE.exists():
        with tempfile.TemporaryDirectory(dir=BASE) as stage:
            raw = Path(stage) / 'formatted.dmg'
            subprocess.run(['/usr/bin/hdiutil','create','-size','98g','-fs','HFS+','-layout','NONE',
                            '-volname','BiomeWorkspace',str(raw)],check=True)
            if raw.stat().st_blocks * 512 < WORKSPACE:
                thick = Path(stage) / 'reserved.dmg'
                allocate(thick, raw.stat().st_size)
                with raw.open('rb') as src, thick.open('r+b') as dst:
                    # Copy the filesystem metadata, keeping zero regions physically reserved.
                    while True:
                        block = src.read(4 * 1024 * 1024)
                        if not block: break
                        if block.count(0) == len(block): dst.seek(len(block),1)
                        else: dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                raw = thick
            os.replace(raw, IMAGE)
            syncdir(BASE)
    if not VAULT.exists():
        with tempfile.TemporaryDirectory(dir=BASE) as stage:
            tmp = Path(stage) / 'reserve'
            allocate(tmp,SACRED)
            with tmp.open('r+b') as f, tarfile.open(fileobj=f,mode='w') as archive: pass
            os.replace(tmp,VAULT)
            syncdir(BASE)
    if not os.path.ismount(MOUNT):
        if any(MOUNT.iterdir()): raise RuntimeError('Unrecognized directory at mountpoint')
        subprocess.run(['/usr/bin/hdiutil','attach','-nobrowse','-owners','on','-mountpoint',str(MOUNT),str(IMAGE)],check=True)
    info = plistlib.loads(subprocess.check_output(['/usr/bin/hdiutil','info','-plist']))
    if not any(Path(i['image-path']).resolve() == IMAGE.resolve() and
               any(e.get('mount-point') == str(MOUNT) for e in i.get('system-entities',[])) for i in info['images']):
        raise RuntimeError('Unexpected backing image at workspace mount')
    marker = MOUNT / MARK
    if not state.exists():
        if not marker.exists(): marker.write_text(uuid.uuid4().hex)
        write_json(state,dict(workspace_uuid=marker.read_text()))
    if not mounted(): raise RuntimeError('Workspace identity mismatch')
    if IMAGE.stat().st_blocks*512 < WORKSPACE or VAULT.stat().st_blocks*512 < SACRED:
        raise RuntimeError('Physical reservation below provisioned capacity')

if __name__ == '__main__': prepare()
