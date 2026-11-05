"""Versioned durable transform jobs. Model work runs in admitted subprocesses."""
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from ..checkpoint import Vault
from ..manifest import read_manifest
from .common import ContextError, digest
from ..inverse import InverseOptions
from .snapshot import snapshot


def identifier(value):
    if not isinstance(value,str) or not value or Path(value).name!=value or value in ('.','..'):
        raise ValueError('Invalid identifier')
    return value


class ContextJobs:
    def __init__(self, root, manifest, launch=True):
        self.root=Path(root);self.jobs=self.root/'context-reconstruction';self.jobs.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.manifest=str(manifest);self.lock=threading.RLock();self.launch=launch
        self.db=sqlite3.connect(self.jobs/'jobs.sqlite',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,version INTEGER,state TEXT,action TEXT,config TEXT,progress TEXT,error TEXT,cancel INTEGER DEFAULT 0);CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,digest TEXT,receipt TEXT);')
        os.chmod(self.jobs/'jobs.sqlite',0o600)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def configuration(self,row):
        return Vault(self.jobs/row['id']/'inputs').load_json('config.enc')

    def _row(self,job_id):
        identifier(job_id)
        row=self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row is None:raise FileNotFoundError(job_id)
        return row

    def get(self, job_id, details=False):
        with self.lock:
            row=self._row(job_id)
            result=dict(run_id=job_id,version=row['version'],status=row['state'],action=row['action'],
                        progress=json.loads(row['progress']) if row['progress'] else None,error=json.loads(row['error']) if row['error'] else None)
        if row['state']=='succeeded':
            data=Vault(self.jobs/job_id/'result').load_json('result.enc')
            result['result']={k:data.get(k) for k in ('status','candidate_kind','best_relative_squared_error','replay_evaluations','source_modified','scientific_reconstruction_success')}
            result['export_available']=True
        return result

    def export(self,body):
        if body.get('allow_plaintext') is not True:
            raise ValueError('Explicit allow_plaintext=true required for context export')
        row=self._row(identifier(body['run_id']))
        if row['state']!='succeeded':raise ValueError('Completed reconstruction required')
        if type(body.get('expected_version')) is not int or body['expected_version']!=row['version']:
            raise ValueError('Stale job version')
        return dict(run_id=row['id'],version=row['version'],artifact=Vault(self.jobs/row['id']/'result').load_json('result.enc'))

    def start(self, action, body):
        if action != 'reconstruct':raise ValueError('Unknown transform job')
        rid=identifier(body.get('request_id'));fp=digest([action,body])
        with self.transaction():
            prior=self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
            if prior:
                if prior['digest']!=fp:raise ValueError('request_id reused with different content')
                return json.loads(prior['receipt'])
            if 'resume_run_id' in body:
                if set(body)-{'request_id','expected_version','resume_run_id'}:raise ValueError('Resume uses frozen inputs')
                job_id=identifier(body['resume_run_id']);row=self._row(job_id)
                if row['action']!=action or type(body.get('expected_version')) is not int or body.get('expected_version')!=row['version'] or row['state'] not in ('cancelled','failed','interrupted'):
                    raise ValueError('Stale version or job is not resumable')
                config=self.configuration(row);version=row['version']+1
                self.db.execute("UPDATE jobs SET version=?,state='queued',error=NULL,cancel=0 WHERE id=?",(version,job_id))
            else:
                if type(body.get('expected_version')) is not int or body['expected_version']!=0:raise ValueError('New job requires expected_version=0')
                if set(body)-{'target_checkpoint','options','purpose','request_id','expected_version'}:
                    raise ValueError('Unknown reconstruction request fields')
                config=dict(options=InverseOptions.parse(body.get('options')).to_dict(),
                            purpose=body.get('purpose','experiment summary'))
                if not isinstance(config['purpose'],str) or not config['purpose'].strip():
                    raise ValueError('purpose required')
                name=identifier(body['target_checkpoint'])
                job_id=uuid.uuid4().hex;version=0
                model=read_manifest(self.manifest,verify=False)
                source=Vault(self.root/'state')
                if source.manifest(name)['metadata']['baseline_hash']!=model['baseline_hash']:
                    raise ValueError('Baseline mismatch')
                from experiments.storage import source_identity
                config['implementation_hash']=source_identity()
                config['baseline_hash']=model['baseline_hash']
                config['source_ref']=snapshot(source,name,Vault(self.jobs/job_id/'source'))
                Vault(self.jobs/job_id/'inputs').save_json('config.enc',config)
                self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL,NULL,0)',(job_id,version,'queued',action,'encrypted'))
            receipt=dict(run_id=job_id,version=version,status='queued')
            self.db.execute('INSERT INTO requests VALUES(?,?,?)',(rid,fp,json.dumps(receipt)))
        if self.launch:self.spawn(job_id)
        return receipt

    def spawn(self,job_id):
        folder=self.jobs/job_id;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (folder/'worker.lock').open('a+b') as handle:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            if self._row(job_id)['state']!='queued':return
            with (folder/'worker.log').open('ab',buffering=0) as log:
                os.chmod(log.name,0o600)
                try:
                    subprocess.Popen([sys.executable,'-m','activation_field.context_reconstruction.jobs','worker',str(self.root),self.manifest,job_id,str(handle.fileno())],pass_fds=(handle.fileno(),),stdout=log,stderr=log,start_new_session=True)
                except Exception as exc:
                    self._finish(job_id,'failed',dict(code='resource_unavailable',message=str(exc)))
                    raise

    def _finish(self, job_id, state, error=None):
        with self.transaction():
            self.db.execute('UPDATE jobs SET state=?,error=?,version=version+1 WHERE id=?',(state,json.dumps(error) if error else None,job_id))

    def cancel(self, job_id, body):
        rid=identifier(body['request_id']);fp=digest(['cancel',job_id,body])
        with self.transaction():
            prior=self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
            if prior:
                if prior['digest']!=fp:raise ValueError('request_id reused')
                return json.loads(prior['receipt'])
            row=self._row(job_id)
            if type(body.get('expected_version')) is not int or body.get('expected_version')!=row['version']:raise ValueError('Stale job version')
            requested=row['state'] in ('queued','running')
            receipt=dict(run_id=job_id,version=row['version']+1,cancel_requested=requested)
            self.db.execute('UPDATE jobs SET cancel=?,version=version+1 WHERE id=?',(int(requested),job_id))
            self.db.execute('INSERT INTO requests VALUES(?,?,?)',(rid,fp,json.dumps(receipt)))
        return receipt

    def reconcile(self):
        # Kernel flock proves whether a detached worker still owns the job.
        # The parent acquires and transfers the lock before spawning. No grace
        # period or arbitrary running-work timeout is required.
        with self.lock:
            rows=list(self.db.execute("SELECT * FROM jobs WHERE state IN ('running','queued')"))
        for row in rows:
            path=self.jobs/row['id']/'worker.lock'
            if row['state']=='queued' and self.launch:
                self.spawn(row['id']);continue
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with path.open('a+b') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:continue
                with self.transaction():
                    if self._row(row['id'])['state'] in ('queued','running'):
                        self.db.execute("UPDATE jobs SET state='interrupted',error=?,version=version+1 WHERE id=?",(json.dumps(dict(code='interrupted',message='Worker exited before recording a terminal result')),row['id']))

    def worker(self, job_id, inherited_fd=None):
        folder=self.jobs/job_id;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (os.fdopen(inherited_fd,'a+b') if inherited_fd is not None else (folder/'worker.lock').open('a+b')) as handle:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            row=self._row(job_id);config=self.configuration(row);action=row['action']
            if row['state'] not in ('queued','running'):return
            with self.db:self.db.execute("UPDATE jobs SET state='running' WHERE id=?",(job_id,))
            last_progress=[0.]
            def check():
                if self._row(job_id)['cancel']:raise ContextError('cancelled','Operator cancellation at safe boundary')
            def progress(value):
                check()
                # Observational throttling only: no compute timeout or truncation.
                if time.monotonic()-last_progress[0]>=1:
                    with self.db:self.db.execute('UPDATE jobs SET progress=? WHERE id=?',(json.dumps(value),job_id))
                    last_progress[0]=time.monotonic()
            engine=None;source=None
            try:
                from experiments.storage import source_identity
                check();model=read_manifest(self.manifest)
                if config['implementation_hash']!=source_identity():raise ContextError('implementation_changed','Resume using the original installed source, or create a new job')
                if model['baseline_hash']!=config['baseline_hash']:raise ContextError('baseline_mismatch','Installed baseline changed')
                from ..inverse_runtime import reconstruct
                from .report import context_artifact
                private=Vault(folder/'replay')
                try:memo=private.load_json('scores.enc')
                except FileNotFoundError:memo={}
                def save_score(key,value):
                    memo[key]=value;private.save_json('scores.enc',memo)
                result=reconstruct(model,Vault(folder/'source'),'source',config['options'],
                                   progress=progress,check=check,memo=memo,save_score=save_score)
                import resource
                result['cost']=dict(peak_process_ram_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                                    job_disk_bytes=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()),
                                    ram_scope='process lifetime high-water mark; not baseline-subtracted',disk_scope='job artifacts, encrypted state and logs')
                result=context_artifact(result,config['source_ref'],config['purpose'],job_id)
                Vault(folder/'result').save_json('result.enc',result);self._finish(job_id,'succeeded')
            except Exception as exc:
                code=getattr(exc,'code','resource_unavailable' if isinstance(exc,MemoryError) else type(exc).__name__)
                self._finish(job_id,'cancelled' if code=='cancelled' else 'failed',dict(code=code,message=str(exc)))


if __name__=='__main__':
    _,mode,root,manifest,job_id,*fds=sys.argv
    if mode!='worker':raise ValueError(mode)
    ContextJobs(root,manifest,launch=False).worker(job_id,int(fds[0]) if fds else None)
