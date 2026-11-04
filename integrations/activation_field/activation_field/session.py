import hashlib,json,secrets,threading,uuid
from pathlib import Path
from .checkpoint import Vault,canonical
from .config import FieldConfig
from .manifest import read_manifest


class Conflict(ValueError):pass


class Sessions:
    def __init__(self,root,manifest):
        self.root=Path(root);self.vault=Vault(self.root/'state')
        self.manifest=read_manifest(manifest)
        self.locks={};self.guard=threading.RLock();self.events={};self.jobs={};self.cancel={}
        self.active=None;self.admission='idle';self.pending_config={};self.job_digests={}
        # Nonpersistent sessions may use encrypted spill while this process is
        # alive, but are explicitly discarded at the next service start.
        for p in (self.vault.root/'sessions').glob('*.enc'):
            s=self.vault.load_json('sessions/'+p.name)
            if not s.get('persist'):
                if s.get('checkpoint'):self.vault.delete(s['checkpoint'])
                self.purge_requests(s['session_id'])
                s.update(closed=True,busy=None,checkpoint=None);self.save(s)
            elif s.get('busy'):
                receipt=self.receipt(s['session_id'],s['busy'])
                receipt.update(status='interrupted',result=dict(code='process_restarted',last_checkpoint=s.get('checkpoint'),committed_token_count=s.get('committed_tokens',0),retryable=False))
                self.vault.save_json(self.request_key(s['session_id'],s['busy']),receipt)
                s['busy']=None;self.save(s)

    def purge_requests(self,sid):
        for p in (self.vault.root/'requests').glob('*.enc'):
            prior=self.vault.load_json('requests/'+p.name)
            if prior.get('session_id')==sid:p.unlink()

    def session_name(self,sid):
        if str(uuid.UUID(sid))!=sid:raise ValueError("Invalid session ID")
        return 'sessions/'+sid+'.enc'

    def get(self,sid):return self.vault.load_json(self.session_name(sid))
    def save(self,s):self.vault.save_json(self.session_name(s['session_id']),s)

    def lock(self,sid):
        with self.guard:return self.locks.setdefault(sid,threading.RLock())

    def request_key(self,sid,rid):
        if not isinstance(rid,str) or not rid or len(rid)>200:raise ValueError("request_id required")
        return 'requests/'+hashlib.sha256((sid+':'+rid).encode()).hexdigest()+'.enc'

    def receipt(self,sid,rid):return self.vault.load_json(self.request_key(sid,rid))

    def begin(self,sid,action,body):
        rid=body['request_id'];key=self.request_key(sid,rid);digest=hashlib.sha256(canonical([action,body])).hexdigest()
        try:prior=self.vault.load_json(key)
        except FileNotFoundError:prior=None
        if prior:
            if prior['digest']!=digest:raise Conflict("request_id_reused_with_different_content")
            return prior,False
        s=self.get(sid)
        if type(body.get('expected_version')) is not int or body['expected_version']!=s['version']:raise Conflict('stale_version')
        if s.get('busy'):raise Conflict('session_busy')
        receipt=dict(request_id=rid,session_id=sid,digest=digest,status='running',action=action,committed_token_count=s.get('committed_tokens',0),version=s['version'])
        self.vault.save_json(key,receipt);s['busy']=rid;self.save(s)
        return receipt,True

    def create(self,body):
        if body.get('expected_version',0)!=0:raise Conflict('New session version must be zero')
        key=self.request_key('create',body['request_id']);digest=hashlib.sha256(canonical(body)).hexdigest()
        with self.guard:
            try:
                prior=self.vault.load_json(key)
                if prior['digest']!=digest:raise Conflict('request_id_reused_with_different_content')
                return prior['result']
            except FileNotFoundError:pass
            config=FieldConfig.parse(body.get('config')).to_dict();sid=str(uuid.uuid4())
            s=dict(session_id=sid,version=0,config=config,checkpoint=None,persist=bool(body.get('persist',False)),baseline_hash=self.manifest['baseline_hash'],busy=None,committed_tokens=0)
            self.save(s);out=dict(session_id=sid,version=0,baseline_hash=s['baseline_hash'],config=config)
            self.vault.save_json(key,dict(digest=digest,result=out));return out

    def finish(self,s,receipt,result,status='succeeded'):
        with self.guard:
            latest=self.get(s['session_id'])
            s['version']=max(s['version'],latest['version'])+1
            if latest['version']>receipt['version']:s['config']=latest['config']
            s['busy']=None;self.save(s)
        receipt.update(status=status,result=result,version=s['version'],committed_token_count=s.get('committed_tokens',0))
        self.vault.save_json(self.request_key(s['session_id'],receipt['request_id']),receipt)
        return receipt

    def configure(self,sid,body):
        # No global guard may be held while waiting for a session lock.
        with self.guard:
            if self.get(sid).get('busy'):return self._configure_busy(sid,body)
        return self.mutate(sid,'config',body)

    def _configure_busy(self,sid,body):
        with self.guard:
            s=self.get(sid)
            rid=body['request_id'];key=self.request_key(sid,rid);digest=hashlib.sha256(canonical(['config',body])).hexdigest()
            try:
                prior=self.vault.load_json(key)
                if prior['digest']!=digest:raise Conflict('request_id_reused_with_different_content')
                return prior
            except FileNotFoundError:pass
            if type(body.get('expected_version')) is not int or body['expected_version']!=s['version']:raise Conflict('stale_version')
            config=s['config'].copy();config.update(body['config']);FieldConfig.parse(config)
            receipt=dict(request_id=rid,session_id=sid,digest=digest,status='queued',action='config',version=s['version'])
            self.vault.save_json(key,receipt)
            self.pending_config.setdefault(sid,[]).append((body,receipt))
            return receipt

    def apply_config(self,sid,engine):
        with self.guard:
            for body,receipt in self.pending_config.pop(sid,[]):
                s=self.get(sid)
                try:
                    if body['expected_version']!=s['version']:raise Conflict('stale_version_at_boundary')
                    engine.patch_config(body['config']);s['config']=engine.config.to_dict();s['version']+=1;self.save(s)
                    receipt.update(status='succeeded',version=s['version'],result=dict(config=s['config'],effective_boundary=engine.consumed_tokens,field_version=engine.field.version))
                except Exception as exc:receipt.update(status='failed',result=dict(code=type(exc).__name__,message=str(exc)))
                self.vault.save_json(self.request_key(sid,receipt['request_id']),receipt)

    def mutate(self,sid,action,body,emit=None,prepared=None):
        with self.lock(sid):
            with self.guard:receipt,fresh=prepared or self.begin(sid,action,body)
            if not fresh:return receipt
            s=self.get(sid);engine=None
            try:
                if s.get('closed'):raise ValueError('Session closed')
                if action=='config':
                    config=s['config'].copy();config.update(body['config']);s['config']=FieldConfig.parse(config).to_dict()
                    return self.finish(s,receipt,dict(config=s['config'],effective_boundary=s['committed_tokens']))
                if action=='delete':
                    if s['checkpoint']:self.vault.delete(s['checkpoint'])
                    self.purge_requests(sid)
                    s['closed']=True
                    return self.finish(s,receipt,dict(closed=True))
                def progress(info):
                    self.admission=info.get('stage','queued')
                    if emit:emit(dict(type='status',**info))
                from .admission import model_slot
                from .runtime import Engine
                with model_slot(progress):
                    self.admission='running';self.active=sid
                    try:
                        engine=Engine(self.manifest,FieldConfig.parse(s['config']))
                        engine.boundary_hook=lambda e:self.apply_config(sid,e)
                        if s['checkpoint']:engine.restore(self.vault,s['checkpoint'],reset_native_state=s.get('reset_native_on_load',False))
                        if action=='generate':
                            result=engine.generate(body['messages'],body.get('max_tokens',128),body.get('temperature',0.),self.cancel.get((sid,body['request_id'])),emit)
                        elif action=='reset':
                            engine.reset(body['kind'],body.get('retain_traces',False));result=engine.diagnostics()
                        elif action=='checkpoint':
                            name='export-'+uuid.uuid4().hex;meta,tensors=engine.state(body.get('field_only',True));result=self.vault.save(name,meta,tensors)
                        elif action=='fork':
                            new=self.create(dict(request_id='fork-'+sid+'-'+body['request_id'],config=s['config'],persist=s['persist']))
                            target=self.get(new['session_id']);name='fork-'+uuid.uuid4().hex
                            meta,tensors=engine.state(body.get('reset_native_state',True));self.vault.save(name,meta,tensors)
                            target['checkpoint']=name;target['reset_native_on_load']=body.get('reset_native_state',True);self.save(target);result=new
                        else:raise ValueError('Unknown action')
                        self.apply_config(sid,engine)
                        name='state-'+uuid.uuid4().hex
                        metadata,tensors=engine.state();self.vault.save(name,metadata,tensors)
                        old=s['checkpoint'];s['checkpoint']=name;s['reset_native_on_load']=False;s['committed_tokens']=engine.consumed_tokens;s['diagnostics']=engine.diagnostics()
                        response=self.finish(s,receipt,result)
                        if old:self.vault.delete(old)
                        return response
                    finally:
                        # Arrays held in local checkpoint iterables also need release.
                        metadata=None;tensors=None;meta=None
                        if engine:engine.close()
                        engine=None;self.active=None;self.admission='idle'
            except Exception as exc:
                # No automatic replay after a partial/crashed request. Saved
                # checkpoint remains authoritative; token loss is explicit.
                s=self.get(sid)
                error=dict(code=type(exc).__name__,message=str(exc)[:300],session_id=sid,request_id=body['request_id'],committed_token_count=s.get('committed_tokens',0),retryable=False,last_checkpoint=s.get('checkpoint'),partial=True)
                return self.finish(s,receipt,error,'failed')

    def start(self,sid,body):
        key=(sid,body['request_id'])
        with self.guard:
            s=self.get(sid)
            if key in self.jobs:
                # Validate content against the original receipt before attaching.
                if self.job_digests[key]!=hashlib.sha256(canonical(['generate',body])).hexdigest():raise Conflict('request_id_reused_with_different_content')
                return
            try:
                prior=self.receipt(sid,body['request_id'])
                if prior['digest']!=hashlib.sha256(canonical(['generate',body])).hexdigest():raise Conflict('request_id_reused_with_different_content')
                return
            except FileNotFoundError:pass
            if body.get('expected_version')!=s['version']:raise Conflict('stale_version')
            if s.get('busy'):raise Conflict('session_busy')
            prepared=self.begin(sid,'generate',body)
            self.job_digests[key]=hashlib.sha256(canonical(['generate',body])).hexdigest()
            self.events[key]=[];self.cancel[key]=threading.Event()
            condition=threading.Condition();self.jobs[key]=condition
            def emit(event):
                with condition:self.events[key].append(event);condition.notify_all()
            def run():
                try:result=self.mutate(sid,'generate',body,emit,prepared=prepared)
                except Exception as exc:result=dict(status='failed',error=dict(code=type(exc).__name__,message=str(exc)))
                emit(dict(type='complete',receipt=result))
            threading.Thread(target=run,name='field-request',daemon=True).start()

    def stream(self,sid,rid,after=0):
        key=(sid,rid);condition=self.jobs.get(key)
        if condition is None:
            yield 'data: '+json.dumps(dict(type='complete',receipt=self.receipt(sid,rid)))+'\n\n';return
        cursor=after
        while True:
            with condition:
                while cursor>=len(self.events[key]):condition.wait()
                events=self.events[key][cursor:]
            for event in events:
                yield 'id: '+str(cursor)+'\ndata: '+json.dumps(event,allow_nan=False)+'\n\n';cursor+=1
                if event['type']=='complete':return
