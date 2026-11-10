import copy
import json
import os
import tempfile
import time
import unittest
import threading
from types import SimpleNamespace
from biomerte.identity import generate, fingerprint
from biomerte.ownership import OwnershipRuntime, CURRENT, PENDING, snapshot
from biomerte.store import Store, Fault, digest, encode


class Registry:
    def __init__(self):
        self.current = None
        self.transfer = None
        self.lost = None
        self.before = None
        self.calls = []

    def call(self, actor, op, data):
        self.calls.append(op)
        if self.before == op:
            self.before = None
            raise OSError('Request not sent')
        if op == 'bootstrap':
            if self.current:
                raise Fault('Already adopted', 409)
            self.current = {k:data[k] for k in ('unit','owner','aliases','authority_digest')}
            self.current['epoch'] = 1
            result = dict(self.current, transfer=None)
        elif op == 'status':
            if not self.current:
                raise Fault('Missing', 410)
            return dict(copy.deepcopy(self.current), transfer=copy.deepcopy(self.transfer))
        elif op == 'prepare':
            if actor != self.current['owner'] or data['epoch'] != self.current['epoch']:
                raise Fault('Not current', 409)
            self.transfer = {k:v for k,v in data.items() if k != 'source_frozen'}
            self.transfer.update(state='prepared', source_ack=False, target_ack=False)
            result = self.transfer
        elif op == 'ready':
            if actor != self.transfer['target'] or self.transfer['state'] not in ('prepared','ready'):
                raise Fault('Not recipient', 403)
            self.transfer['state'] = 'ready'
            result = self.transfer
        elif op == 'commit':
            if self.transfer['state'] != 'ready' or self.transfer['expires'] <= time.time():
                raise Fault('Not ready', 409)
            self.transfer['state'] = 'committed'
            self.current.update(owner=self.transfer['target'], epoch=self.transfer['epoch'] + 1)
            result = self.transfer
        elif op == 'ack':
            self.transfer['source_ack' if actor == self.transfer['owner'] else 'target_ack'] = True
            result = copy.deepcopy(self.transfer)
            if self.transfer['source_ack'] and self.transfer['target_ack']:
                self.transfer = None
        elif op == 'cancel':
            if self.transfer is None:
                self.transfer = dict(data, state='prepared', source_ack=False, target_ack=False)
            if actor != self.transfer['owner'] or self.transfer['state'] == 'committed':
                raise Fault('Cannot cancel', 409)
            self.transfer['state'] = 'aborted'
            self.current['epoch'] = self.transfer['epoch'] + 1
            result = self.transfer
        else:
            raise AssertionError(op)
        if self.lost == op:
            self.lost = None
            raise OSError('Lost response')
        return copy.deepcopy(result)


class OwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.ka = os.path.join(cls.keys.name, 'a')
        cls.kb = os.path.join(cls.keys.name, 'b')
        cls.pa, cls.pb = generate(cls.ka), generate(cls.kb)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sa = Store(self.temp.name, 'a')
        self.sb = Store(self.temp.name, 'b')
        self.a = SimpleNamespace(node=fingerprint(self.pa), site='s', key=self.ka,
                                 config={'public_key':self.pa}, stores={'site':self.sa})
        self.b = SimpleNamespace(node=fingerprint(self.pb), site='s', key=self.kb,
                                 config={'public_key':self.pb}, stores={'site':self.sb})
        self.registry = Registry()
        self.ra = OwnershipRuntime(self.a, lambda op,d:self.registry.call(self.a.node,op,d))
        self.rb = OwnershipRuntime(self.b, lambda op,d:self.registry.call(self.b.node,op,d))
        self.scope = [['offers','offer'], ['work','task']]
        self.rows = [['offers','offer',{'id':'offer','provider':'provider-id','resource':'resource-id','state':'ready'}],
                     ['work','task',{'id':'task','state':'ready','offer':'offer'}]]
        self.sa.execute(lambda tx:[tx.put(*row) for row in self.rows])
        self.aliases = ['offer', 'provider-id', 'resource-id', 'task']

    def adopt(self):
        return self.ra.bootstrap('responsibility', self.aliases, digest({'domain':'execution'}), self.scope, digest(self.rows))

    def prepare(self):
        self.adopt()
        return self.ra.prepare('responsibility', self.pb, self.scope, digest(self.rows),
                               [digest(None)] * len(self.scope), time.time()+1000, digest('one-time operator approval'))

    def stage(self, packet):
        return self.rb.stage(packet, self.pa, digest(packet))

    def read(self, store, kind, key):
        return store.execute(lambda tx:tx.get(kind,key), read_only=True)

    def test_complete_transfer_preserves_pending_work_and_erases_temporary_state(self):
        packet = self.prepare()
        self.stage(packet)
        self.rb.commit()
        self.assertEqual(self.read(self.sb,'work','task')['state'], 'ready')
        self.ra.reconcile()
        self.rb.reconcile()
        self.assertIsNone(self.registry.transfer)
        self.assertIsNone(self.ra.state())
        self.assertIsNone(self.rb.state())
        self.assertIsNone(self.read(self.sa,'work','task'))
        self.assertEqual(set(self.read(self.sa,CURRENT,'responsibility')),
                         {'unit','owner','epoch','authority_digest','aliases','local'})
        self.sb.execute(lambda tx:tx.put('work','next',{'id':'next','provider':'provider-id'}))
        with self.assertRaises(Fault):
            self.sa.execute(lambda tx:tx.put('offers','new',{'provider':'provider-id'}))
        with self.assertRaises(Fault):
            self.sa.execute(lambda tx:tx.put(*self.rows[1]))

    def test_freeze_blocks_responsibility_writes_including_internal_namespaces_only(self):
        self.prepare()
        for kind in ('work','_meta','peers','_session_scope'):
            with self.assertRaises(Fault):
                self.sa.execute(lambda tx:tx.put(kind,'new',{'provider':'provider-id'}))
        self.assertEqual(self.read(self.sa,'work','task')['state'], 'ready')
        self.sa.execute(lambda tx:tx.put('work','unrelated',{'provider':'another-provider'}))

    def test_lost_prepare_reply_preserves_freeze_and_exact_package(self):
        self.adopt()
        self.registry.lost = 'prepare'
        with self.assertRaises(OSError):
            self.ra.prepare('responsibility', self.pb, self.scope, digest(self.rows), [digest(None)]*2,
                            time.time()+1000, digest('approved'))
        self.stage(self.ra.package())
        self.rb.commit()
        self.ra.reconcile()
        self.rb.reconcile()
        self.assertIsNone(self.ra.state())

    def test_lost_commit_reply_reconciles_without_replaying_commit(self):
        packet = self.prepare()
        self.stage(packet)
        self.registry.lost = 'commit'
        with self.assertRaises(OSError):
            self.rb.commit()
        self.rb.reconcile()
        self.ra.reconcile()
        self.rb.reconcile()
        self.assertEqual(self.registry.calls.count('commit'), 1)

    def test_lost_final_ack_is_reconciled_from_current_epoch(self):
        packet = self.prepare()
        self.stage(packet)
        self.rb.commit()
        self.registry.lost = 'ack'
        with self.assertRaises(OSError):
            self.ra.reconcile()
        self.assertIsNone(self.registry.transfer)
        self.ra.reconcile()
        self.rb.reconcile()
        self.assertIsNone(self.ra.state())

    def test_bootstrap_lost_reply_does_not_unfreeze_until_current_row_matches(self):
        self.registry.lost = 'bootstrap'
        with self.assertRaises(OSError):
            self.adopt()
        self.assertEqual(self.ra.state()['phase'], 'bootstrap')
        self.ra.reconcile()
        self.assertIsNone(self.ra.state())

    def test_live_physical_lease_is_never_transferred(self):
        self.sa.execute(lambda tx:tx.put('leases','lease',{'state':'active','provider':'provider-id'}))
        scope = self.scope + [['leases','lease']]
        with self.assertRaises(Fault):
            self.sa.execute(lambda tx:snapshot(tx,scope,self.aliases),read_only=True)
        self.assertIsNone(self.ra.state())

    def test_inbound_and_outbound_dependencies_must_be_in_scope(self):
        with self.assertRaises(Fault):
            self.sa.execute(lambda tx:snapshot(tx,[['offers','offer']],self.aliases),read_only=True)
        with self.assertRaises(Fault):
            self.sa.execute(lambda tx:snapshot(tx,[['work','task']],[]),read_only=True)

    def test_oversize_scan_fails_before_source_freeze(self):
        self.ra.limits['scan_rows'] = 1
        with self.assertRaises(Fault):
            self.adopt()
        self.assertIsNone(self.ra.state())

    def test_wrong_source_key_or_unapproved_package_fails_without_staging(self):
        packet = self.prepare()
        for key, authorization in ((self.pb,digest(packet)), (self.pa,digest('another'))):
            with self.assertRaises(Fault):
                self.rb.stage(packet,key,authorization)
        self.assertIsNone(self.rb.state())

    def test_tampered_signed_snapshot_fails(self):
        packet = self.prepare()
        packet['payload']['data']['records'][1][2]['state'] = 'completed'
        with self.assertRaises(Fault):
            self.stage(packet)
        self.assertIsNone(self.rb.state())

    def test_target_baseline_conflict_and_outside_dependency_block_staging(self):
        packet = self.prepare()
        self.sb.execute(lambda tx:tx.put('work','other',{'resource':'resource-id'}))
        with self.assertRaises(Fault):
            self.stage(packet)
        self.sb.execute(lambda tx:tx.drop('work','other'))
        self.sb.execute(lambda tx:tx.put('work','task',{'state':'completed'}))
        with self.assertRaises(Fault):
            self.stage(packet)
        self.assertIsNone(self.rb.state())

    def test_changed_peer_authority_requires_exact_additional_approval(self):
        self.sa.execute(lambda tx:tx.put('peers','provider-id',{'id':'provider-id','enabled':True,'scopes':['provider.poll']}))
        self.scope.insert(1,['peers','provider-id'])
        self.rows.insert(1,['peers','provider-id',{'id':'provider-id','enabled':True,'scopes':['provider.poll']}])
        packet = self.prepare()
        with self.assertRaises(Fault):
            self.stage(packet)
        self.rb.stage(packet,self.pa,digest(packet),{encode(['peers','provider-id']):digest(self.rows[1][2])})
        self.assertEqual(self.rb.state()['phase'], 'target_staged')

    def test_recipient_can_commit_prepared_handoff_after_source_loss(self):
        packet = self.prepare()
        self.stage(packet)
        self.rb.commit()
        # Source recovery retains its durable fence. There is no deadline steal
        # or claim that unavailable source state was reconstructed.
        self.assertIsNotNone(self.ra.state())
        self.assertTrue(self.rb.reconcile()['pending'])
        self.assertEqual(self.registry.current['owner'],self.b.node)
        self.sb.execute(lambda tx:tx.put('work','after-failover',{'provider':'provider-id','state':'ready'}))

    def test_metadata_is_not_mutable_through_normal_records(self):
        with self.assertRaises(Fault):
            self.sa.execute(lambda tx:tx.put(CURRENT,'fake',{}))

    def test_cancel_staged_transfer_preserves_source_and_does_not_import_target(self):
        packet = self.prepare()
        self.stage(packet)
        self.ra.cancel()
        self.ra.business.execute(lambda tx:tx.put('work','continued',{'provider':'provider-id'}))
        with self.assertRaises(Fault):
            self.rb.commit()
        self.rb.reconcile()
        self.ra.reconcile()
        self.assertIsNone(self.ra.state())
        self.assertIsNone(self.rb.state())
        self.assertIsNone(self.read(self.sb,'work','task'))
        self.assertEqual(self.read(self.sa,'work','task')['state'],'ready')
        self.assertEqual(self.registry.current['owner'], self.a.node)

    def test_unstaged_recipient_can_ack_explicit_source_cancellation(self):
        self.prepare()
        self.ra.cancel()
        self.rb.acknowledge_cancelled('responsibility')
        self.ra.reconcile()
        self.assertIsNone(self.registry.transfer)
        self.assertIsNone(self.ra.state())

    def test_committed_transfer_cannot_be_cancelled(self):
        packet = self.prepare()
        self.stage(packet)
        self.rb.commit()
        with self.assertRaises(Fault):
            self.ra.cancel()

    def test_exact_bootstrap_resume_after_failure_before_remote_execution(self):
        self.registry.before = 'bootstrap'
        with self.assertRaises(OSError):
            self.adopt()
        self.assertIsNone(self.registry.current)
        before = self.ra.state()
        self.ra.resume()
        self.assertIsNone(self.ra.state())
        self.assertEqual(self.registry.current['authority_digest'], before['authority_digest'])

    def test_exact_prepare_resume_and_cancel_before_first_remote_execution(self):
        self.adopt()
        self.registry.before = 'prepare'
        with self.assertRaises(OSError):
            self.ra.prepare('responsibility', self.pb, self.scope, digest(self.rows), [digest(None)]*2,
                            time.time()+1000, digest('approved'))
        before = self.ra.state()
        self.assertIsNone(self.registry.transfer)
        packet = self.ra.resume()
        self.assertEqual(packet['payload']['data']['transfer'],before['transfer'])
        self.ra.cancel()
        self.rb.acknowledge_cancelled('responsibility')
        self.ra.reconcile()
        self.assertIsNone(self.ra.state())

    def test_cancel_can_recover_before_registry_prepare_was_recorded(self):
        self.adopt()
        self.registry.before = 'prepare'
        with self.assertRaises(OSError):
            self.ra.prepare('responsibility', self.pb, self.scope, digest(self.rows), [digest(None)]*2,
                            time.time()+1000, digest('approved'))
        self.ra.cancel()
        self.rb.acknowledge_cancelled('responsibility')
        self.ra.reconcile()
        self.assertEqual(self.registry.current['epoch'],2)

    def test_recipient_active_lease_is_not_overwritten_even_with_exact_baseline(self):
        self.sa.execute(lambda tx:tx.put('leases','lease',{'state':'released','provider':'provider-id'}))
        self.sb.execute(lambda tx:tx.put('leases','lease',{'state':'bound','provider':'provider-id'}))
        self.scope.insert(0,['leases','lease'])
        self.rows.insert(0,['leases','lease',{'state':'released','provider':'provider-id'}])
        self.adopt()
        packet=self.ra.prepare('responsibility',self.pb,self.scope,digest(self.rows),
            [digest({'state':'bound','provider':'provider-id'}),digest(None),digest(None)],time.time()+1000,digest('approved'))
        with self.assertRaises(Fault):
            self.stage(packet)
        self.assertEqual(self.read(self.sb,'leases','lease')['state'],'bound')

    def test_pending_activation_is_not_a_quiescent_snapshot(self):
        self.sa.execute(lambda tx:tx.put('activations','activation',{'state':'pending','provider':'provider-id'}))
        with self.assertRaises(Fault):
            self.sa.execute(lambda tx:snapshot(tx,[['activations','activation']]+self.scope,
                ['activation']+self.aliases),read_only=True)

    def test_stale_stage_and_unstaged_cancellation_ack_are_serialized(self):
        packet=self.prepare()
        reached,release=threading.Event(),threading.Event()
        remote=self.rb.remote
        errors=[]
        def paused(op,data):
            result=remote(op,data)
            if op=='status' and not reached.is_set():
                reached.set()
                if not release.wait(5): raise AssertionError('Test gate timed out')
            return result
        self.rb.remote=paused
        def stage():
            try:self.stage(packet)
            except Exception as e:errors.append(e)
        thread=threading.Thread(target=stage);thread.start()
        self.assertTrue(reached.wait(5))
        self.ra.cancel()
        other=OwnershipRuntime(self.b,remote)
        done=threading.Event()
        def ack():
            try:other.acknowledge_cancelled('responsibility')
            except Exception as e:errors.append(e)
            finally:done.set()
        second=threading.Thread(target=ack);second.start()
        self.assertFalse(done.wait(.05))
        release.set();thread.join(5);second.join(5)
        self.assertFalse(thread.is_alive());self.assertFalse(second.is_alive())
        self.assertIsNotNone(self.registry.transfer)
        self.rb.reconcile();self.ra.reconcile()
        self.assertIsNone(self.rb.state())

    def test_actual_application_request_replay_does_not_recreate_work(self):
        from biomerte.app import Application
        from biomerte.ownership import INERT, CHURN
        for request_id in ('legacy-original', {'stream':0,'sequence':1}):
            # Fresh independent fixtures, with completed local event delivery
            # removed only inside this test before selecting its current scope.
            sa=Store(self.temp.name,'api-a-'+str(type(request_id).__name__))
            sb=Store(self.temp.name,'api-b-'+str(type(request_id).__name__))
            a,b=Application.__new__(Application),Application.__new__(Application)
            a.__dict__.update(self.a.__dict__);b.__dict__.update(self.b.__dict__)
            a.stores={'site':sa};b.stores={'site':sb}
            a.config=dict(a.config,incarnation='a',operational_retention={'enabled':False})
            b.config=dict(b.config,incarnation='b',operational_retention={'enabled':False})
            args={'kind':'task','title':'Transfer once'}
            result=a.call('work.create',args,request_id)
            if isinstance(request_id,str):
                receipt_key=digest(['admin',request_id])
                sa.execute(lambda tx:tx.put('_retention','_receipt:'+receipt_key,
                    {'id':'_receipt:'+receipt_key,'kind':'_receipt','key':receipt_key,'born':time.time(),'terminal_at':time.time()}))
            sa.execute(lambda tx:[tx.drop('events',key) for key, in tx.db.execute("SELECT id FROM records WHERE kind='events'").fetchall()])
            rows=sa.execute(lambda tx:[[kind,key,json.loads(raw)] for kind,key,raw in tx.db.execute("SELECT kind,id,value FROM records WHERE kind!='_meta' ORDER BY kind,id")],read_only=True)
            scope=[row[:2] for row in rows]
            aliases=sorted({key for kind,key,value in rows if kind not in INERT | CHURN})
            registry=Registry()
            source=OwnershipRuntime(a,lambda op,d:registry.call(a.node,op,d))
            target=OwnershipRuntime(b,lambda op,d:registry.call(b.node,op,d))
            source.bootstrap('work-unit',aliases,digest('durable-work'),scope,digest(rows))
            packet=source.prepare('work-unit',self.pb,scope,digest(rows),[digest(None)]*len(scope),time.time()+1000,digest('authorized'))
            target.stage(packet,self.pa,digest(packet))
            target.commit();source.reconcile();target.reconcile()
            if isinstance(request_id,str):
                from biomerte import retention
                for unused in range(8):
                    sa.execute(lambda tx:retention.sweep(tx,now=time.time()+1,seconds=86400,keep=1024,batch=128))
            with self.assertRaises(Fault):a.call('work.create',args,request_id)
            self.assertEqual(b.call('work.create',args,request_id)['id'],result['id'])
            self.assertEqual(sa.execute(lambda tx:len(tx.rows('work')),read_only=True),0)
            self.assertEqual(sb.execute(lambda tx:len(tx.rows('work')),read_only=True),1)
            # Old owner retains only a bounded disposition, never result data.
            kind='_receipt_stream' if isinstance(request_id,dict) else '_receipt'
            disposition=sa.execute(lambda tx:tx.rows(kind),read_only=True)[0]
            self.assertNotIn('result',disposition)
            self.assertNotIn('result_expired',disposition)

    def test_scheduler_skips_frozen_run_and_advances_unrelated_work(self):
        from biomerte import process
        from biomerte.api import Context
        graph={'nodes':{'end':{'type':'end'}},'outputs':{}}
        def run(key):
            return {'id':key,'name':'ordinary','rev':'version','state':'running','tokens':[
                {'id':key+'-token','at':'end','vars':{},'groups':[]}],
                'steps':0,'children':[],'parent':'','task':''}
        def setup(tx):
            tx.ownership_control=True
            tx.put('process_versions','version',{'graph':graph})
            tx.put('runs','frozen-run',run('frozen-run'))
            tx.put('runs','other-run',run('other-run'))
            tx.put(PENDING,'active',{'phase':'source_frozen','scope':[['runs','frozen-run']],
                                     'aliases':['frozen-run']})
        self.sa.execute(setup)
        def tick(tx):
            tx.ownership_execution=True
            try:return process.tick(tx,Context(self.a))
            finally:tx.ownership_execution=False
        self.assertEqual(self.sa.execute(tick),1)
        self.assertEqual(self.read(self.sa,'runs','frozen-run')['state'],'running')
        self.assertEqual(self.read(self.sa,'runs','other-run')['state'],'completed')
        self.assertEqual(len(self.sa.execute(lambda tx:tx.rows('runs'),read_only=True)),2)

    def test_gateway_invalid_identifiers_are_rejected_before_any_freeze(self):
        for unit in ('bad/unit','bad\nunit','bad unit'):
            with self.assertRaises(Fault):
                self.ra.bootstrap(unit,self.aliases,digest('domain'),self.scope,digest(self.rows))
            self.assertIsNone(self.ra.state())
        for scope in ([['bad/kind','key']], [['work','a'*513]], [['work','\uE000'*200]]):
            with self.assertRaises(Fault):
                self.ra.bootstrap('unit',self.aliases,digest('domain'),scope,digest(self.rows))
            self.assertIsNone(self.ra.state())
        with self.assertRaises(Fault):
            self.ra.bootstrap('unit',self.aliases+['bad\nroute'],digest('domain'),self.scope,digest(self.rows))
        self.assertIsNone(self.ra.state())
        self.assertEqual(self.registry.calls,[])

    def test_aggregate_bootstrap_and_prepare_metadata_are_admitted_before_freeze(self):
        aliases=self.aliases+[str(i)+'\uE000'*128 for i in range(124)]
        with self.assertRaises(Fault):
            self.ra.bootstrap('unit',aliases,digest('domain'),self.scope,digest(self.rows))
        self.assertIsNone(self.ra.state())
        self.adopt();before=len(self.registry.calls)
        scope=[['work',str(i)+'\uE000'*160] for i in range(200)]
        with self.assertRaises(Fault):
            self.ra.prepare('responsibility',self.pb,scope,digest(self.rows),[digest(None)]*len(scope),
                            time.time()+1000,digest('approval'))
        self.assertIsNone(self.ra.state())
        self.assertEqual(len(self.registry.calls),before)

    def test_unstaged_cancel_refreshes_prior_foreign_baseline_then_accepts_retry(self):
        packet=self.prepare()
        prior=dict(self.registry.current,local=self.b.node)
        self.rb._control(lambda tx:tx.put(CURRENT,'responsibility',prior))
        self.ra.cancel();self.rb.acknowledge_cancelled('responsibility');self.ra.reconcile()
        self.assertEqual(self.rb._current('responsibility')['epoch'],2)
        packet=self.ra.prepare('responsibility',self.pb,self.scope,digest(self.rows),
                               [digest(None)]*len(self.scope),time.time()+1000,digest('new-approval'))
        self.stage(packet);self.rb.commit();self.ra.reconcile();self.rb.reconcile()
        self.assertEqual(self.rb._current('responsibility')['epoch'],3)

    def test_return_from_another_owner_accepts_stale_foreign_but_not_active_local_baseline(self):
        self.adopt()
        stale=dict(self.registry.current,owner='c'*64,local=self.b.node)
        self.rb._control(lambda tx:tx.put(CURRENT,'responsibility',stale))
        self.registry.current['epoch']=3
        self.ra._control(lambda tx:tx.put(CURRENT,'responsibility',dict(self.registry.current,local=self.a.node)))
        packet=self.ra.prepare('responsibility',self.pb,self.scope,digest(self.rows),
                               [digest(None)]*len(self.scope),time.time()+1000,digest('return'))
        self.stage(packet)
        self.assertEqual(self.rb.state()['phase'],'target_staged')
        # A locally active conflicting responsibility is not a mere stale cache.
        other=OwnershipRuntime(self.b,self.rb.remote)
        self.rb._control(lambda tx:(tx.drop(PENDING,'active'),tx.put(CURRENT,'responsibility',dict(stale,owner=self.b.node))))
        with self.assertRaises(Fault):other.stage(packet,self.pa,digest(packet))
        self.assertIsNone(other.state())

    def test_in_place_adoption_preserves_active_operation_and_does_not_fence_progress(self):
        active=['actions','running',{'id':'running','state':'running','resource':'resource-id','progress':0}]
        self.sa.execute(lambda tx:tx.put(*active))
        rows=sorted(self.rows+[active],key=lambda row:row[:2])
        original=self.ra.remote
        def remote(op,data):
            if op=='bootstrap':
                pending=self.ra.state()
                self.assertEqual(pending['phase'],'adoption_pending')
                self.assertNotIn('records',pending)
                self.assertNotIn('scope',pending)
                self.sa.execute(lambda tx:tx.put('actions','running',dict(active[2],progress=1)))
            return original(op,data)
        self.ra.remote=remote
        result=self.ra.adopt_current('responsibility',self.aliases,digest('qualified sole-owner plan'),
                                     [row[:2] for row in rows],digest(rows))
        self.assertEqual(result,{'adopted':'responsibility','epoch':1})
        self.assertEqual(self.read(self.sa,'actions','running')['progress'],1)
        self.assertEqual(self.read(self.sa,'work','task'),self.rows[1][2])
        self.assertIsNone(self.ra.state())
        self.assertIsNone(self.rb._current('responsibility'))
        # Registering existing custody does not make an active effect portable.
        latest=self.sa.execute(lambda tx:[[kind,key,tx.need(kind,key)] for kind,key in [row[:2] for row in rows]],read_only=True)
        with self.assertRaises(Fault):
            self.ra.prepare('responsibility',self.pb,[row[:2] for row in rows],digest(latest),
                            [digest(None)]*len(rows),time.time()+1000,digest('handoff'))
        self.assertIsNone(self.ra.state())

    def test_in_place_adoption_exact_resume_after_pre_send_failure(self):
        self.registry.before='bootstrap'
        with self.assertRaises(OSError):
            self.ra.adopt_current('responsibility',self.aliases,digest('plan'),self.scope,digest(self.rows))
        recorded=copy.deepcopy(self.ra.state())
        self.sa.execute(lambda tx:tx.put('work','task',dict(self.rows[1][2],progress=1)))
        result=self.ra.resume()
        self.assertEqual(result,{'adopted':'responsibility','epoch':1})
        self.assertEqual(self.read(self.sa,'work','task')['progress'],1)
        self.assertEqual(self.ra._current('responsibility')['authority_digest'],recorded['authority_digest'])
        self.assertIsNone(self.ra.state())

    def test_in_place_adoption_reconciles_lost_response_without_replay_or_snapshot(self):
        self.registry.lost='bootstrap'
        with self.assertRaises(OSError):
            self.ra.adopt_current('responsibility',self.aliases,digest('plan'),self.scope,digest(self.rows))
        self.assertNotIn('records',self.ra.state())
        self.ra.reconcile()
        self.assertEqual(self.registry.calls.count('bootstrap'),1)
        self.assertIsNone(self.ra.state())

    def test_in_place_adoption_rejects_wrong_witness_alias_and_foreign_authority(self):
        for aliases,expected in [(self.aliases,digest('wrong')), (self.aliases+['unproven'],digest(self.rows))]:
            with self.assertRaises(Fault):
                self.ra.adopt_current('responsibility',aliases,digest('plan'),self.scope,expected)
            self.assertIsNone(self.ra.state())
        self.assertEqual(self.registry.calls,[])
        foreign={'unit':'other','owner':self.b.node,'local':self.a.node,'epoch':1,
                 'authority_digest':digest('other'),'aliases':self.aliases}
        self.ra._control(lambda tx:tx.put(CURRENT,'other',foreign))
        with self.assertRaises(Fault):
            self.ra.adopt_current('responsibility',self.aliases,digest('plan'),self.scope,digest(self.rows))
        self.assertIsNone(self.ra.state())
        self.assertEqual(self.registry.calls,[])


if __name__ == '__main__':
    unittest.main()
