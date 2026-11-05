import json, tempfile, unittest
from pathlib import Path
from biomerte.worker import Worker
from biomerte.store import Fault, digest, encode

class Remote:
    site='site'; realm='site'; node='worker'
    def __init__(self):
        self.queue={'fences': [], 'actions': [], 'activations': []}
        self.records={}; self.ack=[]
    def call(self,name,args,rid=None):
        if name=='provider.poll': return self.queue
        if name=='cap.status': return self.records[args['operation']]
        if name=='cap.fenced': self.ack.append(args); return {}
        raise AssertionError(name)

class WorkerRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.remote=Remote()
        self.w=Worker(str(Path(self.tmp.name)/'worker.sqlite'),self.remote,
                      [{'resource':'terminal','contract':'terminal:1','argv':['/bin/true']}],None)
        self.addCleanup(self.w.db.close)
    def uncertain(self, state='uncertain', quiescent=False):
        a=dict(id='old',provider='worker',contract='terminal:1',inputs={'argv':['/bin/true']},
               resource='terminal',guard='slot-a',generation=1,lease='prior',state=state,
               result={'reconciliation':{'quiescent':quiescent}})
        self.remote.records['old']=a
        self.w.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                          ('old',digest([a['contract'],a['inputs'],a['resource'],1,'prior']),
                           'uncertain',encode({'reason':'original uncertainty'}),'slot-a'))
        return a
    def fence(self):
        return dict(id='new',resource='terminal',guard='slot-a',generation=2)
    def test_bad_fence_does_not_block_another_slot(self):
        self.uncertain()
        self.remote.queue={'activations': [],'fences':[self.fence()],
                           'actions':[dict(id='blocked',resource='terminal',guard='slot-a'),
                                      dict(id='independent',resource='terminal',guard='slot-b')]}
        seen=[]; self.w.perform=lambda a: seen.append(a['id'])
        self.w.once()
        self.assertEqual(seen,['independent'])
        self.assertEqual(self.remote.ack,[])
        h=json.loads((Path(self.tmp.name)/'worker-health.json').read_text())
        self.assertEqual(h['state'],'degraded')
        self.assertEqual(h['blocked'][0]['guard'],'slot-a')
    def test_bad_fence_does_not_block_pool_dispatch(self):
        self.uncertain()
        self.remote.queue={'activations':[],'fences':[self.fence()],
                           'actions':[dict(id='independent',resource='terminal',guard='slot-b')]}
        class Pool:
            failures={}
            def dispatch(s,actions): s.actions=actions; return len(actions)
        pool=Pool();self.w.once(pool)
        self.assertEqual([a['id'] for a in pool.actions],['independent'])
    def test_quiescent_reconciliation_repairs_local_ledger_before_fence(self):
        a=self.uncertain('failed',True);self.w.fence(self.fence())
        row=self.w.db.execute("SELECT state,result FROM actions WHERE id='old'").fetchone()
        self.assertEqual(row,('failed',encode(a['result'])))
        self.assertEqual(len(self.remote.ack),1)
        self.assertIsNotNone(self.w.db.execute("SELECT previous_result FROM reconciliations WHERE id='old'").fetchone())
    def test_terminal_without_quiescence_does_not_clear_uncertainty(self):
        self.uncertain('failed',False)
        with self.assertRaises(Fault): self.w.fence(self.fence())
        self.assertEqual(self.remote.ack,[])
    def test_identity_mismatch_does_not_clear_uncertainty(self):
        a=self.uncertain('failed',True);a['inputs']={'argv':['different']}
        with self.assertRaises(Fault):self.w.fence(self.fence())
        self.assertEqual(self.remote.ack,[])
    def test_foreign_provider_does_not_clear_uncertainty(self):
        a=self.uncertain('failed',True);a['provider']='foreign'
        with self.assertRaises(Fault):self.w.fence(self.fence())
    def test_unrelated_failed_action_does_not_abort_sequential_worker(self):
        self.remote.queue['actions']=[dict(id='bad',resource='terminal',guard='slot-a'),
                                      dict(id='good',resource='terminal',guard='slot-b')]
        seen=[]
        def perform(a):
            if a['id']=='bad':raise Fault('Failed item')
            seen.append(a['id'])
        self.w.perform=perform;self.w.once();self.assertEqual(seen,['good'])
    def test_new_action_cannot_bypass_uncertain_slot(self):
        self.uncertain()
        a=dict(id='new-action',resource='terminal',guard='slot-a',generation=1,lease='prior')
        with self.assertRaises(Fault):self.w.perform(a)
