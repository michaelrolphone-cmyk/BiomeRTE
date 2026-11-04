"""Host-wide FIFO model admission. Metadata only; never stores inference inputs.

All Biome inference adapters share this module and its OS lock. A crashed owner
releases the lock automatically. Dead queued processes are removed by identity,
never by an arbitrary duration or model-generation deadline.
"""
import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import URLError

ROOT = Path.home()/'.local/share/biome-model-runtime'

class ModelSelectionError(ValueError):
    public_code='selected_model_unavailable'

class ModelLoadError(RuntimeError):
    public_code='model_load_failed'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Local model redirect refused')


def process_identity(pid):
    try:
        os.kill(pid, 0)
        value = subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True).strip()
        return value or None
    except (ProcessLookupError, subprocess.CalledProcessError):
        return None


class LocalModels:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.http = build_opener(ProxyHandler({}), NoRedirect())
        self.lm_state = Path.home()/'.local/share/biome-lmstudio'

    def config(self):
        return json.loads((self.root/'config.json').read_text())

    def lm(self, path, body=None):
        headers = {'Content-Type':'application/json'}
        token = self.lm_state/'api-token'
        if token.exists(): headers['Authorization']='Bearer '+token.read_text().strip()
        req = Request('http://127.0.0.1:1234'+path,
                      None if body is None else json.dumps(body).encode(), headers)
        with self.http.open(req) as response:
            return json.load(response)

    def comfy(self, path, body=None):
        req = Request('http://127.0.0.1:8188'+path,
                      None if body is None else json.dumps(body).encode(),
                      {'Content-Type':'application/json'})
        try:
            with self.http.open(req) as response:
                return json.load(response)
        except URLError as exc:
            # A refused connection has no resident ComfyUI process on this port.
            # Other connection errors remain unknown and cannot authorize loading.
            if isinstance(exc.reason, ConnectionRefusedError):
                return {'loaded_models':0,'running':0,'pending':0}
            raise

    def wait_image_idle(self, progress=None):
        while True:
            state = self.comfy('/biome/model-memory')
            if not state['running'] and not state['pending']: return
            if progress: progress({'stage':'queued','waiting_for':'image'})
            time.sleep(.5)

    def unload_images(self):
        state = self.comfy('/biome/model-memory', {'unload_models':True})
        if state.get('loaded_models') != 0:
            raise RuntimeError('ComfyUI models are still resident')

    def models(self):
        return self.lm('/api/v1/models')['models']

    def unload_llms(self, keep=None):
        for model in self.models():
            for instance in model.get('loaded_instances', []):
                if instance['id'] != keep:
                    self.lm('/api/v1/models/unload', {'instance_id':instance['id']})
        if any(i['id'] != keep for m in self.models() for i in m.get('loaded_instances', [])):
            raise RuntimeError('Competing LM Studio models are still resident')

    def select(self, requested):
        if not isinstance(requested,str) or not requested:
            raise ValueError('An explicit model is required')
        models = self.models()
        matches = [m for m in models if m.get('type')=='llm' and
                   (m['key']==requested or any(i['id']==requested for i in m.get('loaded_instances',[])))]
        if not matches: raise ModelSelectionError('Requested model is unavailable')
        # Duplicate catalog entries may share a key, but different models cannot.
        keys = {m['key'] for m in matches}
        if len(keys)!=1: raise ModelSelectionError('Requested model is ambiguous')
        key = matches[0]['key']
        instances = [i['id'] for m in models if m.get('key')==key for i in m.get('loaded_instances',[])]
        keep = requested if requested in instances else next(iter(instances),None)
        self.unload_llms(keep)
        if keep is None:
            cfg = self.config()
            body = {'model':key}
            if key==cfg['default_llm_model']: body.update(cfg.get('default_load',{}))
            try:
                keep = self.lm('/api/v1/models/load',body)['instance_id']
            except Exception:
                raise ModelLoadError('Requested model could not be loaded') from None
        confirmed = self.models()
        if not any(m.get('key')==key and any(i['id']==keep for i in m.get('loaded_instances',[])) for m in confirmed):
            raise ModelLoadError('Requested model load was not confirmed')
        return keep

    def prepare(self, kind, model, progress=None):
        self.wait_image_idle(progress)
        if kind=='image':
            self.unload_llms()
            return None
        self.unload_images()
        return self.select(model)

    def release(self, kind):
        if kind=='image':
            self.wait_image_idle()
            self.unload_images()

    def warm_default(self):
        state = self.comfy('/biome/model-memory')
        if state['running'] or state['pending']: return False
        if any(m.get('loaded_instances') for m in self.models()):
            # Keep an explicitly selected conversation model warm until another
            # queued task requests a different model; don't undo the user's choice.
            return False
        self.unload_images()
        self.select(self.config()['default_llm_model'])
        return True


class ModelRuntime:
    def __init__(self, root=ROOT, models=None, alive=process_identity):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.models = models or LocalModels(self.root)
        self.alive = alive
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS queue(ticket INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,pid INTEGER,birth TEXT,kind TEXT,model TEXT,phase TEXT)')

    def db(self):
        return sqlite3.connect(str(self.root/'admission.sqlite'))

    def clean(self, db):
        for ident,pid,birth in db.execute('SELECT id,pid,birth FROM queue').fetchall():
            if self.alive(pid)!=birth: db.execute('DELETE FROM queue WHERE id=?',(ident,))

    def state(self):
        with self.db() as db:
            return [dict(zip(('ticket','id','pid','kind','model','phase'),row)) for row in
                    db.execute('SELECT ticket,id,pid,kind,model,phase FROM queue ORDER BY ticket')]

    @contextlib.contextmanager
    def slot(self, kind, model=None, progress=None):
        if kind not in ('llm','image'): raise ValueError('Unknown workload kind')
        if kind=='llm' and not model: raise ValueError('Explicit model required')
        ident=uuid.uuid4().hex;pid=os.getpid();birth=self.alive(pid)
        if birth is None: raise RuntimeError('Cannot identify model worker')
        with self.db() as db:
            self.clean(db)
            db.execute('INSERT INTO queue(id,pid,birth,kind,model,phase) VALUES(?,?,?,?,?,?)',
                       (ident,pid,birth,kind,model,'queued'))
        lock=open(self.root/'active.lock','a+b');locked=False
        try:
            while not locked:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with self.db() as db:
                        self.clean(db)
                        head=db.execute('SELECT id,kind FROM queue ORDER BY ticket LIMIT 1').fetchone()
                    if head and head[0]==ident:
                        locked=True
                    else:
                        fcntl.flock(lock,fcntl.LOCK_UN)
                except BlockingIOError:
                    pass
                if not locked:
                    if progress: progress({'stage':'queued','waiting_for':'model_memory'})
                    time.sleep(.2)
            with self.db() as db: db.execute("UPDATE queue SET phase='preparing' WHERE id=?",(ident,))
            selected=self.models.prepare(kind,model,progress)
            with self.db() as db: db.execute("UPDATE queue SET phase='running' WHERE id=?",(ident,))
            try:
                yield selected
            finally:
                # Cleanup runs while holding the model lock. A failed cleanup
                # propagates; the next owner must verify unloading before loading.
                self.models.release(kind)
        finally:
            with self.db() as db: db.execute('DELETE FROM queue WHERE id=?',(ident,))
            if locked: fcntl.flock(lock,fcntl.LOCK_UN)
            lock.close()

    def warm_default(self):
        with open(self.root/'active.lock','a+b') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return False
            with self.db() as db:
                self.clean(db)
                if db.execute('SELECT 1 FROM queue LIMIT 1').fetchone(): return False
            return self.models.warm_default()


def run_idle():
    runtime=ModelRuntime()
    while True:
        try: runtime.warm_default()
        except Exception as exc:
            # No prompts, credentials, model responses or HTTP bodies in logs.
            print('Model idle preparation waiting: '+type(exc).__name__,flush=True)
        time.sleep(2)

if __name__=='__main__': run_idle()
