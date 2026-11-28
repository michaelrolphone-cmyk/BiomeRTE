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
from .common import TransformError, digest
from .fit import configuration


def identifier(value):
    if not isinstance(value,str) or not value or Path(value).name!=value or value in ('.','..'):
        raise ValueError('Invalid identifier')
    return value


class TransformJobs:
    def __init__(self, root, manifest, launch=True):
        self.root=Path(root);self.jobs=self.root/'transforms';self.jobs.mkdir(parents=True,exist_ok=True,mode=0o700)
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
        result['result']=None
        vault=Vault(self.jobs/job_id/'result')
        if (vault.root/'result.enc').exists():
            data=vault.load_json('result.enc')
            if details:result['result']=data
            elif row['action']=='fit':
                report=data.get('report',{})
                result['result']=dict(stop_reason=data['stop_reason'],candidates=[{k:r[k] for k in ('candidate_id','bytes','sha256','region_count','validation_loss','above_goal')} for r in data['candidates']],
                                      metrics=report.get('global_metrics'),behavioral_evaluation=report.get('behavioral_evaluation'))
            elif row['action']=='compare':result['result']={k:data.get(k) for k in ('scores','paired_kl','retention_ratio','paired_bootstrap_95','scientific_usefulness','assessment','selected_candidate')}
            else:result['result']={k:data[k] for k in ('checkpoint_id','sha256','native_state','writes','decay') if k in data}
            if not details:result['result']['cost']=data.get('cost')
        elif row['action']=='fit' and (self.jobs/job_id/'fit/private/fit.enc').is_file():
            partial=Vault(self.jobs/job_id/'fit/private').load_json('fit.enc')
            result['result']=dict(partial=True,stop_reason=partial.get('stop_reason'),
                candidates=[{k:r[k] for k in ('candidate_id','bytes','sha256','region_count','validation_loss','above_goal')} for r in partial.get('candidates',[])],
                completed_tensors=partial.get('tensor_index',0),behavioral_evaluation='unexecuted')
        return result

    def artifact(self, run_id, candidate_id):
        identifier(run_id);identifier(candidate_id)
        row=self._row(run_id)
        if row['action']!='fit':raise ValueError('Artifact must come from a fit job')
        path=self.jobs/run_id/'fit'/(candidate_id+'.afwt.enc')
        if not path.is_file():raise FileNotFoundError(candidate_id)
        return path

    def inspect(self, body):
        from .format import inspect_transform
        return inspect_transform(self.artifact(body['run_id'],body['candidate_id']))

    def export(self, body):
        import base64
        from .format import read_artifact
        path=self.artifact(body['run_id'],body['candidate_id'])
        return dict(**self.inspect(body),filename=body['candidate_id']+'.afwt',encoding='base64',data=base64.b64encode(read_artifact(path)).decode())

    def start(self, action, body):
        if action not in ('fit','materialize','compare'):raise ValueError('Unknown transform job')
        rid=identifier(body.get('request_id'));fp=digest([action,body])
        with self.transaction():
            prior=self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
            if prior:
                if prior['digest']!=fp:raise ValueError('request_id reused with different content')
                return json.loads(prior['receipt'])
            if 'resume_run_id' in body:
                if set(body)-{'request_id','expected_version','resume_run_id'}:raise ValueError('Resume uses frozen inputs')
                job_id=identifier(body['resume_run_id']);row=self._row(job_id)
                if row['action']!=action or body.get('expected_version')!=row['version'] or row['state'] not in ('cancelled','failed','interrupted'):
                    raise ValueError('Stale version or job is not resumable')
                config=self.configuration(row);version=row['version']+1
                self.db.execute("UPDATE jobs SET version=?,state='queued',error=NULL,cancel=0 WHERE id=?",(version,job_id))
            else:
                if type(body.get('expected_version')) is not int or body['expected_version']!=0:raise ValueError('New job requires expected_version=0')
                fields={'fit':{'target_checkpoint','config'},'materialize':{'fit_run_id','candidate_id'},'compare':{'target_checkpoint','restored_checkpoint','suite'}}[action]
                if set(body)-fields-{'request_id','expected_version'}:raise ValueError('Unknown transform request fields')
                config={k:body[k] for k in fields if k in body}
                if action=='fit':config['config']=configuration(config.get('config'))
                for name in ('target_checkpoint','restored_checkpoint'):
                    if name in config:identifier(config[name])
                if action=='fit' and 'target_checkpoint' not in config:raise ValueError('target_checkpoint required')
                if action=='materialize':
                    artifact=self.inspect(dict(run_id=config['fit_run_id'],candidate_id=config['candidate_id']))
                    config['artifact_hash']=artifact['sha256']
                if action=='compare' and not all(k in config for k in fields):raise ValueError('Missing comparison input')
                job_id=uuid.uuid4().hex;version=0
                model=read_manifest(self.manifest,verify=False)
                config['baseline_hash']=model['baseline_hash']
                v=Vault(self.root/'state')
                for name in ('target_checkpoint','restored_checkpoint'):
                    if name in config:config[name+'_hash']=digest(v.manifest(config[name]))
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
                    subprocess.Popen([sys.executable,'-m','activation_field.transforms.jobs','worker',str(self.root),self.manifest,job_id,str(handle.fileno())],pass_fds=(handle.fileno(),),stdout=log,stderr=log,start_new_session=True)
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
            if body.get('expected_version')!=row['version']:raise ValueError('Stale job version')
            requested=row['state'] in ('queued','running')
            receipt=dict(run_id=job_id,version=row['version']+1,cancel_requested=requested)
            self.db.execute('UPDATE jobs SET cancel=?,version=version+1 WHERE id=?',(int(requested),job_id))
            self.db.execute('INSERT INTO requests VALUES(?,?,?)',(rid,fp,json.dumps(receipt)))
        return receipt

    def assess(self,job_id,body):
        rid=identifier(body['request_id']);fp=digest(['assessment',job_id,body])
        with self.transaction():
            prior=self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
            if prior:
                if prior['digest']!=fp:raise ValueError('request_id reused')
                return json.loads(prior['receipt'])
            row=self._row(job_id)
            if row['action']!='compare' or row['state']!='succeeded':raise ValueError('Completed comparison required')
            if body.get('expected_version')!=row['version']:raise ValueError('Stale job version')
            if not all(isinstance(body.get(k),str) and body[k].strip() for k in ('observations','attribution')):
                raise ValueError('Observations and attribution required')
            vault=Vault(self.jobs/job_id/'result');report=vault.load_json('result.enc')
            report['assessment']=dict(observations=body['observations'],attribution=body['attribution'],blind_labels=body.get('blind_labels'))
            report['selected_candidate']=body.get('selected_candidate');vault.save_json('result.enc',report)
            receipt=dict(recorded=True,run_id=job_id,version=row['version']+1,scientific_usefulness='operator assessment; see attributed observations')
            self.db.execute('UPDATE jobs SET version=version+1 WHERE id=?',(job_id,))
            self.db.execute('INSERT INTO requests VALUES(?,?,?)',(rid,fp,json.dumps(receipt)))
            return receipt

    def trial(self,job_id,body,sessions):
        """Three independent read-only sessions; optional labels hide mapping."""
        rid=identifier(body['request_id']);fp=digest(['trial',job_id,body])
        with self.transaction():
            prior=self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
            if prior:
                if prior['digest']!=fp:raise ValueError('request_id reused')
                return json.loads(prior['receipt'])
            row=self._row(job_id)
            if row['action']!='compare' or row['state']!='succeeded':raise ValueError('Completed comparison required')
            if body.get('expected_version')!=row['version']:raise ValueError('Stale job version')
            cfg=self.configuration(row);private=Vault(self.jobs/job_id/'trials')
            name=digest(rid)+'.enc'
            if (private.root/name).exists():mapping=private.load_json(name)
            else:
                labels=['Baseline','Evolved','Restored']
                if body.get('blind',False):
                    import secrets
                    labels=['A','B','C'];secrets.SystemRandom().shuffle(labels)
                mapping=dict(zip(labels,[None,cfg['target_checkpoint'],cfg['restored_checkpoint']]))
                private.save_json(name,mapping)
            states={}
            for label,checkpoint in sorted(mapping.items()):
                if hasattr(sessions,'open_trial'):
                    opened=sessions.open_trial(checkpoint,rid+'-'+label)
                    states[label]=dict(session_id=opened['session_id'],version=opened['version'])
                    continue
                config=dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False)
                created=sessions.create(dict(request_id=rid+'-'+label,expected_version=0,config=config,persist=True))
                state=sessions.get(created['session_id'])
                if checkpoint and not state.get('checkpoint'):
                    destination='transform-trial-'+created['session_id']
                    if not (sessions.vault.root/destination).exists():
                        meta=sessions.vault.manifest(checkpoint)['metadata']
                        sessions.vault.save(destination,meta,sessions.vault.arrays(checkpoint))
                    state['checkpoint']=destination;state['reset_native_on_load']=True;sessions.save(state)
                states[label]=dict(session_id=created['session_id'],version=state['version'])
            meta=sessions.vault.manifest(cfg['restored_checkpoint'])['metadata']
            receipt=dict(run_id=job_id,version=row['version']+1,sessions=states,blind=bool(body.get('blind',False)),
                         artifact_sha256=meta.get('source_transform'),artifact_bytes=meta.get('source_transform_bytes'),writes=False,decay=False)
            self.db.execute('UPDATE jobs SET version=version+1 WHERE id=?',(job_id,))
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
                if self._row(job_id)['cancel']:raise TransformError('cancelled','Operator cancellation at safe boundary')
            def progress(value):
                check()
                # Observational throttling only: no compute timeout or truncation.
                if time.monotonic()-last_progress[0]>=1:
                    with self.db:self.db.execute('UPDATE jobs SET progress=? WHERE id=?',(json.dumps(value),job_id))
                    last_progress[0]=time.monotonic()
            engine=None;source=None
            try:
                check();model=read_manifest(self.manifest)
                if model['baseline_hash']!=config['baseline_hash']:raise TransformError('baseline_mismatch','Installed baseline changed')
                state=Vault(self.root/'state')
                for name in ('target_checkpoint','restored_checkpoint'):
                    if name in config and digest(state.manifest(config[name]))!=config[name+'_hash']:
                        raise TransformError('source_version_changed','Source checkpoint changed')
                from ..admission import model_slot
                from ..runtime import Engine
                from ..config import FieldConfig
                from .source import RuntimeSource
                with model_slot(progress):
                    try:
                        check();engine=Engine(model,FieldConfig.parse(dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False)))
                        engine.boundary_hook=lambda _:check()
                        if action=='fit':
                            from .fit import fit
                            source=RuntimeSource(engine,state,config['target_checkpoint'],tile_bytes=config['config']['tile_bytes'],check=check)
                            result=fit(source,folder/'fit',config['config'],resume=(folder/'fit/private/fit.enc').exists(),progress=progress,cancel=check)
                        elif action=='materialize':
                            from .materialize import materialize
                            artifact=self.artifact(config['fit_run_id'],config['candidate_id'])
                            if self.inspect(dict(run_id=config['fit_run_id'],candidate_id=config['candidate_id']))['sha256']!=config['artifact_hash']:
                                raise TransformError('source_version_changed','Selected artifact changed')
                            source=RuntimeSource(engine,state,check=check)
                            result=materialize(source,artifact,state,'transform-'+job_id,folder,resume=(folder/'materialization/progress.enc').exists(),progress=progress,cancel=check)
                        else:
                            from .compare import compare
                            result=compare(engine,state,config['target_checkpoint'],config['restored_checkpoint'],config['suite'],folder/'comparison',resume=(folder/'comparison/comparison.enc').exists(),progress=progress,cancel=check)
                    finally:
                        # Tiled readers retain projection modules; release them
                        # before Engine.close collects arrays and admission exits.
                        source=None
                        if engine:engine.close()
                        engine=None
                import resource
                result['cost']=dict(peak_process_ram_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                                    job_disk_bytes=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()),
                                    ram_scope='process lifetime high-water mark; not baseline-subtracted',disk_scope='job artifacts, encrypted state and logs')
                Vault(folder/'result').save_json('result.enc',result);self._finish(job_id,'succeeded')
            except Exception as exc:
                code=getattr(exc,'code','resource_unavailable' if isinstance(exc,MemoryError) else type(exc).__name__)
                self._finish(job_id,'cancelled' if code=='cancelled' else 'failed',dict(code=code,message=str(exc)))


if __name__=='__main__':
    _,mode,root,manifest,job_id,*fds=sys.argv
    if mode!='worker':raise ValueError(mode)
    TransformJobs(root,manifest,launch=False).worker(job_id,int(fds[0]) if fds else None)
