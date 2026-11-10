import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from biomerte.identity import generate, signed
from biomerte.store import Store, Fault, digest
from biomerte.session_exchange import identity, import_package
from biomerte.session_runtime import SessionRuntime
from tests.test_session_exchange import work_value


class Gateway:
    def __init__(self):
        self.row = None
        self.entries = []
        self.calls = []
        self.lose = None
        self.caller = 'a'

    def call_as(self,node,operation,args):
        prior = self.caller
        self.caller = node
        try:return self(operation,args)
        finally:self.caller = prior

    def __call__(self, operation, args):
        self.calls.append(operation)
        if operation == 'open':
            self.row = dict(args, owner=self.caller,seq=0,hash='',state='open',closer=None,closer_epoch=0,closer_until=0,
                            definition=json.dumps({'participants':args['participants'],'scope':args['scope'],
                            'authorized_closers':sorted(args.get('authorized_closers',[self.caller])),
                            'successor_after':args.get('successor_after',args['expires'])}))
            result = self.row
        elif self.row is None:
            raise Fault('Session unavailable',410)
        elif operation == 'status':
            result = self.row
        elif operation == 'append':
            entry=json.loads(args['entry'])
            if args['expected']!=self.row['seq']:raise Fault('Conflict',409)
            hashed=hashlib.sha256(args['entry'].encode()).hexdigest()
            self.entries.append({'seq':entry['seq'],'hash':hashed,'entry':args['entry']})
            self.row.update(seq=entry['seq'],hash=hashed)
            result={'seq':entry['seq'],'hash':hashed}
        elif operation == 'read':
            rows=[r for r in self.entries if r['seq']>args['after']]
            result={'record':rows[0] if rows else None,'final_seq':self.row['seq'],'final_hash':self.row['hash'],'state':self.row['state'],'closer':self.row['closer'],
                    'closer_epoch':self.row['closer_epoch'],'closer_until':self.row['closer_until']}
        elif operation == 'freeze':
            assert args['final_seq']==self.row['seq'] and args['final_hash']==self.row['hash']
            definition=json.loads(self.row['definition'])
            if self.caller not in definition['authorized_closers'] or (self.caller!=self.row['owner'] and time.time()<definition['successor_after']):
                raise Fault('Closer not eligible',403)
            if self.row['closer'] and self.row['closer']!=self.caller and self.row['closer_until']>time.time():
                raise Fault('Other live closer',409)
            if self.row['closer']!=self.caller or self.row['closer_until']<=time.time():
                self.row['closer_epoch']+=1
            self.row.update(state='frozen',closer=self.caller,closer_until=time.time()+60)
            result={'final_seq':self.row['seq'],'final_hash':self.row['hash'],'closer':self.row['closer'],
                    'closer_epoch':self.row['closer_epoch'],'closer_until':self.row['closer_until']}
        elif operation == 'outbox':
            assert args['durable'] and len(args['packages'])==len(self.row['participants'])
            if self.caller!=self.row['closer'] or args['closer_epoch']!=self.row['closer_epoch'] or self.row['closer_until']<=time.time():
                raise Fault('Stale closer',409)
            self.row=None;self.entries=[];result={'closed':True,'pending_delivery':['b']}
        elif operation == 'close':
            self.row=None;self.entries=[];result={'closed':True,'pending_delivery':['b']}
        else:raise AssertionError(operation)
        if self.lose==operation:
            self.lose=None
            raise OSError('Lost reply')
        return result


class SessionRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=tempfile.TemporaryDirectory()
        cls.ka=os.path.join(cls.keys.name,'a');cls.kb=os.path.join(cls.keys.name,'b')
        cls.pa=generate(cls.ka);cls.pb=generate(cls.kb)

    @classmethod
    def tearDownClass(cls):cls.keys.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.a=SimpleNamespace(node='a',site='s',key=self.ka,config={'public_key':self.pa})
        self.b=SimpleNamespace(node='b',site='s',key=self.kb,config={'public_key':self.pb})
        self.business=Store(self.temp.name,'business');self.target=Store(self.temp.name,'target')
        self.target.execute(lambda tx:tx.put('peers','a',{'key':self.pa,'enabled':True,'scopes':['coordination']}))
        self.business.execute(lambda tx:tx.put('peers','b',{'key':self.pb,'enabled':True,'scopes':['coordination']}))
        self.gateway=Gateway();self.runtime=SessionRuntime(self.a,self.temp.name,self.gateway,self.business)
        self.baselines={n:{identity('work','task'):digest(None)} for n in ('a','b')}

    def open(self, policy=None):
        return self.runtime.open([['work','task']],['a','b'],self.baselines,{'a':self.pa,'b':self.pb},policy)

    def deliver(self,node,packet):
        if packet['payload']['kind'] == 'session-acceptance':
            return self.participant().settle(packet)
        self.assertEqual(node,'b');state=copy.deepcopy(self.runtime.state());state['state']='closing'
        proof = self.target.execute(lambda tx:import_package(tx,self.b,state,packet))
        return signed(self.kb, {'format':1,'site':'s','sender':'b','to':'a','kind':'session-ack','data':proof})

    def test_complete_cycle_deletes_shared_history_and_local_session(self):
        self.open();self.runtime.append([['work','task',work_value(large=9007199254740993)]])
        result=self.runtime.finalize();self.assertTrue(result['closed']);self.assertIsNone(self.gateway.row)
        self.assertEqual(self.runtime.pending_recipients(),['b'])
        self.runtime.deliver_pending(self.deliver)
        self.assertIsNone(self.runtime.state())
        self.assertEqual(self.business.execute(lambda tx:tx.get('work','task'),read_only=True),self.target.execute(lambda tx:tx.get('work','task'),read_only=True))
        with self.runtime.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],0)

    def test_lost_append_reply_reconciles_without_repeating_write(self):
        self.open();self.gateway.lose='append'
        with self.assertRaises(OSError):self.runtime.append([['work','task',work_value()]])
        self.runtime.synchronize()
        self.assertEqual(self.gateway.calls.count('append'),1)
        self.assertIsNone(self.runtime.store.execute(lambda tx:tx.get('_coordination','pending'),read_only=True))

    def test_lost_close_reply_recovers_from_local_outbox(self):
        self.open();self.runtime.append([['work','task',work_value()]])
        self.gateway.lose='outbox'
        with self.assertRaises(OSError):self.runtime.finalize()
        self.assertTrue(self.runtime.finalize()['closed'])
        self.assertEqual(self.gateway.calls.count('outbox'),1)
        self.runtime.deliver_pending(self.deliver)
        self.assertIsNone(self.runtime.state())

    def test_lost_open_reply_uses_status_not_duplicate_open(self):
        self.gateway.lose='open'
        with self.assertRaises(OSError):self.open()
        self.assertEqual(self.runtime.recover_open()['state'],'open')
        self.assertEqual(self.gateway.calls.count('open'),1)

    def test_sequence_bound_drives_export_and_deletion(self):
        self.open({'max_entries':1,'seconds':3600,'max_bytes':4096})
        self.runtime.append([['work','task',work_value()]])
        self.assertTrue(self.runtime.maintain()['closed'])
        self.assertIsNone(self.gateway.row)

    def test_ordinary_work_stays_local_without_session(self):
        self.business.execute(lambda tx:tx.put('work','ordinary',work_value()))
        self.assertEqual(self.gateway.calls,[])
        self.assertEqual(self.runtime.maintain(),{'active':False})

    def test_high_churn_rejected_before_network(self):
        with self.assertRaises(Fault):self.runtime.open([['leases','x']],['a','b'],{}, {})
        self.assertEqual(self.gateway.calls,[])

    def participant(self):
        directory = os.path.join(self.temp.name, 'participant')
        return SessionRuntime(self.b, directory, self.gateway, self.target)

    def test_signed_join_and_participant_final_receipt(self):
        self.open()
        participant = self.participant()
        invitation = self.runtime.invitation('b')
        participant.join(invitation)
        self.assertEqual(participant.join(invitation)['id'], self.runtime.state()['id'])
        participant.append([['work', 'task', work_value(state='accepted', evidence={'done':'checked'})]])
        with self.assertRaises(Fault):
            participant.finalize()
        self.runtime.finalize()
        self.runtime.deliver_pending(lambda node, packet: participant.settle(packet) if packet['payload']['kind']=='session-acceptance' else participant.receive(packet))
        self.assertIsNone(participant.state())
        self.assertIsNone(self.runtime.state())
        self.assertIsNone(self.gateway.row)

    def test_lost_delivery_ack_survives_later_local_edit_and_long_partition(self):
        from unittest.mock import patch
        self.open()
        participant = self.participant()
        participant.join(self.runtime.invitation('b'))
        self.runtime.append([['work', 'task', work_value(state='accepted')]])
        self.runtime.finalize()
        def lose(node, packet):
            participant.receive(packet)
            raise OSError('Lost delivery reply')
        with self.assertRaises(OSError):
            self.runtime.deliver_pending(lose)
        self.target.execute(lambda tx: tx.put('work', 'task', {'state': 'accepted'}))
        packet = self.runtime.store.execute(lambda tx: tx.need('_outbox', 'b'), read_only=True)
        self.runtime.deliver_pending(lambda node, packet: participant.settle(packet) if packet['payload']['kind']=='session-acceptance' else participant.receive(packet))
        self.assertEqual(self.target.execute(lambda tx: tx.need('work', 'task'), read_only=True)['state'], 'accepted')
        import time
        with patch('biomerte.session_runtime.time.time', return_value=time.time() + 86401):
            participant.maintain()
            with self.assertRaises(Fault):
                participant.receive(packet)
        self.assertEqual(self.target.execute(lambda tx: tx.rows('_session_delivery_ack'), read_only=True), [])

    def test_tampered_expired_and_changed_baseline_invitations(self):
        self.open()
        participant = self.participant()
        invitation = self.runtime.invitation('b')
        tampered = copy.deepcopy(invitation)
        tampered['payload']['data']['expires'] += 30
        with self.assertRaises(Fault):
            participant.join(tampered)
        expired = copy.deepcopy(invitation['payload'])
        expired['data']['expires'] = 0
        with self.assertRaises(Fault):
            participant.join(signed(self.ka, expired))
        self.target.execute(lambda tx: tx.put('work','task',{'state':'local-change'}))
        with self.assertRaises(Fault):
            participant.join(invitation)
        self.assertIsNone(participant.state())

    def test_open_not_admitted_can_be_aborted_without_replay(self):
        def disconnected(operation, args):
            if operation == 'open':
                raise OSError('No response')
            return self.gateway(operation, args)
        self.runtime.remote = disconnected
        with self.assertRaises(OSError):
            self.open()
        self.assertEqual(self.runtime.recover_open(), {'active':False,'opened':False})
        self.assertIsNone(self.runtime.state())
        self.assertNotIn('open', self.gateway.calls)

    def test_outbox_recovery_does_not_reimport_over_new_local_work(self):
        self.open()
        self.runtime.append([['work','task',work_value(state='accepted')]])
        real = self.runtime.remote
        def fail_before_commit(operation, args):
            if operation == 'outbox':
                raise OSError('Request never reached gateway')
            return real(operation, args)
        self.runtime.remote = fail_before_commit
        with self.assertRaises(OSError):
            self.runtime.finalize()
        with self.assertRaises(Fault):
            self.business.execute(lambda tx: tx.put('work','task',work_value(revision=2)))
        self.runtime.remote = real
        self.assertTrue(self.runtime.finalize()['closed'])
        self.assertEqual(self.business.execute(lambda tx: tx.need('work','task'), read_only=True)['state'], 'accepted')

    def test_byte_bound_automatically_closes_without_admitting_write(self):
        self.open({'max_entries':10, 'seconds':3600, 'max_bytes':100})
        with self.assertRaises(Fault):
            self.runtime.append([['work', 'task', work_value(body='x'*100)]])
        self.assertIsNone(self.gateway.row)
        self.assertEqual(self.runtime.state()['state'], 'delivery_pending')
        self.assertEqual(self.gateway.calls.count('append'), 0)

    def test_real_knowledge_revision_lifecycle_and_immutable_closure(self):
        from biomerte import knowledge
        from biomerte.api import Context
        self.a.config['incarnation'] = 'test'
        ctx = Context(self.a, scope='local')
        # Prepare real domain revisions locally; only durable rows are exported.
        staging = Store(self.temp.name, 'staging')
        first = staging.execute(lambda tx: knowledge.put(tx, ctx, 'guide', {'v': 1}))
        second = staging.execute(lambda tx: knowledge.put(tx, ctx, 'guide', {'v': 2}, [first['rev']]))
        scope = [['documents', 'guide'], ['revisions', first['rev']], ['revisions', second['rev']]]
        baselines = {n: {identity(*pair): digest(None) for pair in scope} for n in ('a','b')}
        self.runtime.open(scope, ['a','b'], baselines, {'a':self.pa,'b':self.pb})
        participant = self.participant()
        participant.join(self.runtime.invitation('b'))
        rows = staging.execute(lambda tx: [[kind, key, tx.need(kind,key)] for kind,key in scope], read_only=True)
        self.runtime.append(rows)
        mutated = copy.deepcopy(first); mutated['body'] = {'v': 99}
        with self.assertRaises(Fault):
            participant.append([['revisions', first['rev'], mutated]])
        with self.assertRaises(Fault):
            participant.append([['revisions', first['rev'], None]])
        with self.assertRaises(Fault):
            participant.append([['documents','guide',{'id':'guide','heads':['missing'],'conflict':False}]])
        self.runtime.finalize()
        self.runtime.deliver_pending(lambda node, packet: participant.settle(packet) if packet['payload']['kind']=='session-acceptance' else participant.receive(packet))
        got = self.target.execute(lambda tx: knowledge.get(tx, Context(self.b), 'guide'), read_only=True)
        self.assertEqual(got['heads'], [second['rev']])
        self.assertEqual(got['revisions'][0]['body'], {'v':2})
        next_revision = self.business.execute(lambda tx:knowledge.put(tx,ctx,'guide',{'v':3},[second['rev']]))
        self.assertEqual(next_revision['seq'], second['seq'] + 1)
        self.assertEqual(self.target.execute(lambda tx: tx.rows('events'), read_only=True), [])
        self.assertIsNone(self.gateway.row)

    def test_real_process_work_grant_outcome_lifecycle(self):
        from biomerte import work, process, exchange
        from biomerte.api import Context
        self.a.config['incarnation'] = 'test'
        ctx = Context(self.a, scope='local')
        staging = Store(self.temp.name, 'domain')
        staging.execute(lambda tx: tx.put('peers', 'b', {'key':self.pb,'enabled':True,'scopes':['return']}))
        item = staging.execute(lambda tx: work.create(tx, ctx, 'task', 'Do the job', id='task', criteria=['verified']))
        published = staging.execute(lambda tx: process.publish(tx, ctx, 'job', {'start':'end','nodes':{'end':{'type':'end','result':{}}}}))
        grant_packet = staging.execute(lambda tx: exchange.delegate(tx, ctx, 'task', 'job', {}, 'b', []))
        grant_id = grant_packet['payload']['data']['id']
        offline = Store(self.temp.name, 'offline-worker')
        offline.execute(lambda tx: tx.put('peers','a',{'key':self.pa,'enabled':True,'scopes':['delegate']}))
        worker_ctx = Context(self.b, scope='local')
        incoming = offline.execute(lambda tx: exchange.accept(tx, worker_ctx, grant_packet))
        offline.execute(lambda tx: process.tick(tx, worker_ctx))
        outcome = offline.execute(lambda tx: exchange.report(tx, worker_ctx, incoming['id']), read_only=True)
        staging.execute(lambda tx: exchange.reconcile(tx, ctx, outcome))
        accepted = staging.execute(lambda tx: work.accept(tx, ctx, 'task', 3, {'verified':'Checked output'}))
        scope = [['work','task'],['definitions','job'],['process_versions',published['rev']],['grants',grant_id]]
        baseline = {n:{identity(*p):digest(None) for p in scope} for n in ('a','b')}
        self.runtime.open(scope,['a','b'],baseline,{'a':self.pa,'b':self.pb})
        participant = self.participant(); participant.join(self.runtime.invitation('b'))
        rows = staging.execute(lambda tx:[[kind,key,tx.need(kind,key)] for kind,key in scope], read_only=True)
        traced = copy.deepcopy(rows)
        for kind, key, value in traced:
            if kind == 'grants':
                value['outcome']['trace'] = [{'activity':'temporary-runtime-detail'}]
        before = self.gateway.calls.count('append')
        with self.assertRaises(Fault):
            self.runtime.append(traced)
        self.assertEqual(self.gateway.calls.count('append'), before)
        self.runtime.append(rows)
        self.runtime.finalize()
        self.runtime.deliver_pending(lambda node, packet:participant.settle(packet) if packet['payload']['kind']=='session-acceptance' else participant.receive(packet))
        self.assertEqual(self.target.execute(lambda tx:tx.need('work','task'),read_only=True),accepted)
        self.assertEqual(self.target.execute(lambda tx:tx.need('grants',grant_id),read_only=True)['state'],'returned')
        self.assertEqual(self.target.execute(lambda tx:tx.rows('runs'),read_only=True),[])
        with self.assertRaises(Fault):
            self.runtime.open([['incoming','local-run-receipt']],['a','b'],{}, {})

    def test_freeze_reconciles_uncertain_unaccepted_append(self):
        self.open()
        remote = self.runtime.remote
        def fail_before_append(operation, args):
            if operation == 'append':
                raise OSError('Append request outcome unknown')
            return remote(operation, args)
        self.runtime.remote = fail_before_append
        with self.assertRaises(OSError):
            self.runtime.append([['work','task',work_value()]])
        self.assertIsNotNone(self.runtime.store.execute(lambda tx:tx.get('_coordination','pending'),read_only=True))
        self.runtime.remote = remote
        self.runtime.finalize()
        self.assertIsNone(self.runtime.store.execute(lambda tx:tx.get('_coordination','pending'),read_only=True))
        self.runtime.deliver_pending(self.deliver)
        self.assertIsNone(self.runtime.state())

    def test_forged_delivery_ack_does_not_discard_outbox(self):
        self.open(); self.runtime.append([['work','task',work_value()]])
        self.runtime.finalize()
        def forged(node, packet):
            ack = self.deliver(node, packet)
            ack['payload']['data']['package_digest'] = '0'*64
            return ack
        with self.assertRaises(Fault):
            self.runtime.deliver_pending(forged)
        self.assertEqual(self.runtime.pending_recipients(), ['b'])

    def test_invalid_or_stale_work_rejected_before_append(self):
        self.open()
        with self.assertRaises(Fault):
            self.runtime.append([['work','task',{'state':'accepted'}]])
        self.assertNotIn('append', self.gateway.calls)
        self.runtime.append([['work','task',work_value()]])
        with self.assertRaises(Fault):
            self.runtime.append([['work','task',work_value(title='Changed without revision')]])
        self.runtime.append([['work','task',work_value(title='Proper edit', revision=2)]])

    def test_existing_document_branch_cannot_be_omitted(self):
        from biomerte import knowledge
        from biomerte.api import Context
        self.a.config['incarnation'] = 'test'
        revision = self.business.execute(lambda tx:knowledge.put(tx,Context(self.a,scope='local'),'guide',{'v':1}))
        doc = self.business.execute(lambda tx:tx.need('documents','guide'),read_only=True)
        baselines = {'a':{identity('documents','guide'):digest(doc)},'b':{identity('documents','guide'):digest(None)}}
        with self.assertRaises(Fault):
            self.runtime.open([['documents','guide']],['a','b'],baselines,{'a':self.pa,'b':self.pb})
        self.assertEqual(self.gateway.calls, [])

    def successor_pair(self):
        state=self.runtime.open([['work','task']],['a','b'],self.baselines,{'a':self.pa,'b':self.pb},
            {'max_entries':16,'seconds':10,'max_bytes':65536},authorized_closers=['b','a'])
        other=self.participant()
        other.remote=lambda op,args:self.gateway.call_as('b',op,args)
        other.join(self.runtime.invitation('b'))
        return state,other

    @staticmethod
    def receive_all(runtime,packet):
        return runtime.settle(packet) if packet['payload']['kind']=='session-acceptance' else runtime.receive(packet)

    def test_successor_closes_after_bound_owner_returns_and_imports(self):
        from unittest.mock import patch
        state,other=self.successor_pair()
        self.runtime.append([['work','task',work_value()]])
        with patch('time.time',return_value=state['expires']+1):
            self.assertTrue(other.maintain()['closed'])
            self.assertIsNone(self.gateway.row)
            other.deliver_pending(lambda node,packet:self.receive_all(self.runtime,packet))
        self.assertIsNone(self.runtime.state())
        self.assertIsNone(other.state())
        self.assertEqual(self.business.execute(lambda tx:tx.need('work','task'),read_only=True)['state'],'accepted')

    def test_expired_loser_outbox_yields_to_actual_successor_after_delete(self):
        from unittest.mock import patch
        state,other=self.successor_pair()
        self.runtime.append([['work','task',work_value()]])
        real=self.runtime.remote
        def no_attestation(op,args):
            if op=='outbox':raise OSError('Crash before attestation')
            return real(op,args)
        self.runtime.remote=no_attestation
        with self.assertRaises(OSError):self.runtime.finalize()
        self.assertTrue(self.runtime.state()['outbox_ready'])
        first_epoch=self.runtime.state()['closer_epoch']
        with patch('time.time',return_value=state['expires']+100):
            self.assertTrue(other.finalize()['closed'])
            self.assertGreater(other.state()['closer_epoch'],first_epoch)
            self.runtime.remote=real
            self.assertTrue(self.runtime.finalize()['close_uncertain'])
            self.assertEqual(self.runtime.state()['state'],'close_uncertain')
            other.deliver_pending(lambda node,packet:self.receive_all(self.runtime,packet))
        self.assertIsNone(other.state());self.assertIsNone(self.runtime.state())
        self.assertEqual(self.runtime.pending_recipients(),[])

    def test_own_delete_lost_reply_past_lease_remains_explicitly_uncertain(self):
        from unittest.mock import patch
        state,other=self.successor_pair()
        self.runtime.append([['work','task',work_value()]])
        self.gateway.lose='outbox'
        with self.assertRaises(OSError):self.runtime.finalize()
        self.assertIsNone(self.gateway.row)
        with patch('time.time',return_value=state['expires']+100):
            self.assertTrue(self.runtime.finalize()['close_uncertain'])
            calls=list(self.gateway.calls)
            self.runtime.maintain()
            self.assertEqual(calls,self.gateway.calls)
            report=self.runtime.inspect_reconciliation()['payload']['data']
            self.assertEqual(report['state'],'close_uncertain')
            self.assertEqual(len(report['pending_outbox']),2)
            with self.assertRaises(Fault):self.runtime.deliver_pending(lambda *args:None)

    def test_competing_closer_and_lost_freeze_reply_reconcile_epoch(self):
        from unittest.mock import patch
        state,other=self.successor_pair()
        self.runtime.append([['work','task',work_value()]])
        self.gateway.lose='freeze'
        with self.assertRaises(OSError):self.runtime.finalize()
        epoch=self.gateway.row['closer_epoch']
        with patch('time.time',return_value=state['expires']+1):
            self.assertEqual(other.finalize()['awaiting_closer'],'a')
        self.assertTrue(self.runtime.finalize()['closed'])
        self.assertEqual(self.runtime.state()['closer_epoch'],epoch)
        self.runtime.deliver_pending(lambda node,packet:self.receive_all(other,packet))

    def test_local_scope_guard_and_reserving_crash_recovery(self):
        from unittest.mock import patch
        real=self.runtime._reserve_scope
        def crash(tx,state):
            real(tx,state)
        # Persist reserving intent and reservation, then fail mirror initialization.
        original=self.runtime.store.execute
        calls=[0]
        def interrupted(fn,*args,**kwargs):
            calls[0]+=1
            if calls[0]==3:raise OSError('Crash before mirror initialization')
            return original(fn,*args,**kwargs)
        # Directly reproduce exact durable crash boundary without remote effects.
        state={'id':'interrupted','state':'reserving','scope':[['work','task']],
               'baselines':self.baselines,'participants':['a','b']}
        self.runtime._set(state)
        self.business.execute(lambda tx:real(tx,state))
        with self.assertRaises(Fault):self.business.execute(lambda tx:tx.put('work','task',work_value()))
        self.assertEqual(self.gateway.calls,[])
        self.assertFalse(self.runtime.maintain()['active'])
        self.business.execute(lambda tx:tx.put('work','task',work_value()))
        self.assertEqual(self.business.execute(lambda tx:tx.rows('_session_scope'),read_only=True),[])

    def test_open_work_and_running_work_cannot_enter_session(self):
        from biomerte import process,work
        from biomerte.api import Context
        self.open()
        for state in ('open','running','delegated','review'):
            with self.assertRaises(Fault):self.runtime.append([['work','task',work_value(state=state)]])
        self.assertNotIn('append',self.gateway.calls)
        self.runtime.append([['work','task',work_value()]])
        self.runtime.finalize();self.runtime.deliver_pending(self.deliver)
        graph={'start':'end','nodes':{'end':{'type':'end','result':{}}}}
        for app,store in ((self.a,self.business),(self.b,self.target)):
            ctx=Context(app,scope='local')
            store.execute(lambda tx:process.publish(tx,ctx,'job',graph))
            with self.assertRaises(Fault):store.execute(lambda tx:process.start(tx,ctx,'job',{},task='task'))

    def test_packet_and_total_outbox_limits_reject_before_gateway_write(self):
        policy={'max_entries':10,'seconds':3600,'max_bytes':1048576,'packet_bytes':22000,'outbox_bytes':44000}
        self.open(policy)
        with self.assertRaises(Fault):self.runtime.append([['work','task',work_value(blob='x'*10000)]])
        self.assertNotIn('append',self.gateway.calls)
        self.assertIsNotNone(self.gateway.row)
        with self.assertRaises(Fault):self.runtime.append([['work','task',work_value(blob='x'*1048576)]])

    def test_accepted_parent_cannot_ignore_existing_unaccepted_child(self):
        parent=work_value(id='parent',kind='goal')
        self.target.execute(lambda tx:tx.put('work','child',work_value(id='child',kind='milestone',parent='parent',state='open')))
        scope=[['work','parent']]
        baseline={n:{identity('work','parent'):digest(None)} for n in ('a','b')}
        self.runtime.open(scope,['a','b'],baseline,{'a':self.pa,'b':self.pb})
        self.runtime.append([['work','parent',parent]])
        self.runtime.finalize()
        with self.assertRaises(Fault):self.runtime.deliver_pending(self.deliver)
        self.assertIsNone(self.target.execute(lambda tx:tx.get('work','parent'),read_only=True))

    def test_three_party_higher_outcome_ack_preserves_later_local_edits(self):
        from unittest.mock import patch
        key=os.path.join(self.temp.name,'c.key');public=generate(key)
        app=SimpleNamespace(node='c',site='s',key=key,config={'public_key':public})
        store=Store(os.path.join(self.temp.name,'c'),'business')
        for node,pub in (('a',self.pa),('b',self.pb)):
            store.execute(lambda tx:tx.put('peers',node,{'key':pub,'enabled':True,'scopes':['coordination']}))
        self.business.execute(lambda tx:tx.put('peers','c',{'key':public,'enabled':True,'scopes':['coordination']}))
        baseline={n:{identity('work','task'):digest(None)} for n in ('a','b','c')}
        state=self.runtime.open([['work','task']],['a','b','c'],baseline,{'a':self.pa,'b':self.pb,'c':public},
            {'max_entries':16,'seconds':10,'max_bytes':65536},authorized_closers=['b','a'])
        b=self.participant();b.remote=lambda op,args:self.gateway.call_as('b',op,args)
        c=SessionRuntime(app,os.path.join(self.temp.name,'c','coordination'),
            lambda op,args:self.gateway.call_as('c',op,args),store)
        b.join(self.runtime.invitation('b'));c.join(self.runtime.invitation('c'))
        self.runtime.append([['work','task',work_value()]])
        real=self.runtime.remote
        def fail(op,args):
            if op=='outbox':raise OSError('Interrupted closer')
            return real(op,args)
        self.runtime.remote=fail
        with self.assertRaises(OSError):self.runtime.finalize()
        stale=self.runtime.store.execute(lambda tx:tx.need('_outbox','c'),read_only=True)
        c.receive(stale)  # Earlier import proof is still genuinely unsettled.
        later=work_value(title='Later local edit',revision=2)
        store.execute(lambda tx:tx.put('work','task',later))
        with patch('time.time',return_value=state['expires']+100):
            b.finalize();self.runtime.remote=real
            self.assertTrue(self.runtime.finalize()['close_uncertain'])
            b.deliver_pending(lambda node,packet:self.receive_all(self.runtime if node=='a' else c,packet))
        self.assertEqual(store.execute(lambda tx:tx.need('work','task'),read_only=True),later)
        self.assertEqual(store.execute(lambda tx:tx.rows('_session_delivery_ack'),read_only=True),[])
        self.assertIsNone(self.runtime.state());self.assertIsNone(b.state());self.assertIsNone(c.state())

    def test_join_requires_every_authorized_successor_locally_trusted(self):
        key=os.path.join(self.temp.name,'c.key');public=generate(key)
        app=SimpleNamespace(node='c',site='s',key=key,config={'public_key':public})
        store=Store(os.path.join(self.temp.name,'c'),'business')
        store.execute(lambda tx:tx.put('peers','a',{'key':self.pa,'enabled':True,'scopes':['coordination']}))
        self.business.execute(lambda tx:tx.put('peers','c',{'key':public,'enabled':True,'scopes':['coordination']}))
        baseline={n:{identity('work','task'):digest(None)} for n in ('a','b','c')}
        self.runtime.open([['work','task']],['a','b','c'],baseline,{'a':self.pa,'b':self.pb,'c':public},authorized_closers=['b','a'])
        c=SessionRuntime(app,os.path.join(self.temp.name,'c','coordination'),self.gateway,store)
        with self.assertRaises(Fault):c.join(self.runtime.invitation('c'))
        self.assertIsNone(c.state())
        store.execute(lambda tx:tx.put('peers','b',{'key':self.pb,'enabled':True,'scopes':['coordination']}))
        self.assertEqual(c.join(self.runtime.invitation('c'))['authorized_closers'],['a','b'])
