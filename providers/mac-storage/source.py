"""Read-only Pi SSH forced command. One transactionally consistent SQLite snapshot per transfer."""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

CHUNK = 1024 * 1024
def send(value):
    sys.stdout.buffer.write((json.dumps(value) + '\n').encode())
    sys.stdout.buffer.flush()

def main():
    cfg=json.load(open('/home/pi/.local/share/biomerte-file-storage/manifest.json'))
    root=cfg['mount']
    if not os.path.ismount(root): raise RuntimeError('Pi source is offline')
    src=os.path.join(root,'biomerte-files','files.sqlite')
    request=json.loads(sys.stdin.buffer.readline(4096).decode())
    db=sqlite3.connect('file:'+src+'?mode=ro',uri=True,timeout=20,isolation_level=None)
    with tempfile.TemporaryFile(dir='/var/tmp') as snap:
        try:
            db.execute('BEGIN')
            if db.execute('SELECT uuid FROM volume').fetchall()!=[(cfg['volume'],)]:raise ValueError('Wrong Pi volume')
            if db.execute('PRAGMA journal_mode').fetchone()[0]!='delete':
                raise RuntimeError('Source journal mode changed; cannot copy rollback-mode database')
            st=os.stat(src)
            stamp=[cfg['volume'],st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
            if request.get('stamp')==stamp:
                send({'same':True,'volume':cfg['volume']})
                return
            with open(src,'rb') as f:
                shutil.copyfileobj(f,snap,CHUNK)
        finally: db.close()
        total=hashlib.sha256()
        chunks=[]
        snap.seek(0)
        while True:
            b=snap.read(CHUNK)
            if not b:break
            chunks.append(hashlib.sha256(b).hexdigest())
            total.update(b)
        send({'volume':cfg['volume'],'stamp':stamp,'size':snap.tell(),'sha256':total.hexdigest(),'chunks':chunks})
        wanted=json.loads(sys.stdin.buffer.readline(65536).decode())
        if not isinstance(wanted,list) or len(set(wanted))!=len(wanted) or not all(type(i) is int and 0<=i<len(chunks) for i in wanted):
            raise ValueError('Invalid chunk indices')
        for i in wanted:
            snap.seek(i*CHUNK)
            sys.stdout.buffer.write(snap.read(CHUNK))
        sys.stdout.buffer.flush()

if __name__=='__main__':main()
