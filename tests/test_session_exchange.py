import copy
import os
import tempfile
import unittest
from types import SimpleNamespace
from biomerte.identity import generate, signed
from biomerte.store import Store, Fault, digest, encode
from biomerte.session_exchange import export_package, import_package, identity, validate_scope


def work_value(**fields):
    value = {'id':'task', 'kind':'task', 'title':'Verify task', 'parent':'',
             'priority':0, 'sequence':0, 'dependencies':[], 'criteria':[], 'context':[],
             'state':'accepted', 'revision':1, 'assigned':'', 'attempts':[],
             'evidence':{'checked':'Verified output'}}
    value.update(fields)
    return value


class SessionExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key_a = os.path.join(cls.keys.name, 'a.key')
        cls.key_b = os.path.join(cls.keys.name, 'b.key')
        cls.pub_a, cls.pub_b = generate(cls.key_a), generate(cls.key_b)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a = SimpleNamespace(node='a', site='s', key=self.key_a)
        self.b = SimpleNamespace(node='b', site='s', key=self.key_b)
        self.source, self.target = Store(self.temp.name, 'a'), Store(self.temp.name, 'b')
        self.target.execute(lambda tx: tx.put('peers', 'a', {'id':'a','key':self.pub_a,'enabled':True,'scopes':['coordination']}))
        self.head = {'seq':3, 'hash':'f'*64}
        baseline = {identity('work','task'):digest(None)}
        self.session = {'id':'unique-session','state':'closing','participants':['a','b'],
                        'scope':[['work','task']], 'final_head':self.head,
                        'baselines':{'a':dict(baseline),'b':dict(baseline)},
                        'peer_keys':{'a':self.pub_a,'b':self.pub_b}}

    def packet(self, value):
        self.source.execute(lambda tx: tx.put('work','task',value))
        return self.source.execute(lambda tx: export_package(tx,self.a,self.session,'b',self.head),read_only=True)

    def receive(self, packet, session=None):
        return self.target.execute(lambda tx: import_package(tx,self.b,session or self.session,packet))

    def test_real_signature_and_lossless_numeric_roundtrip(self):
        value=work_value(large=9007199254740993, whole=1.0, negative_zero=-0.0, unicode='雪', nested=[1.0,9007199254740993])
        packet=self.packet(value)
        self.receive(packet)
        with self.target.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM records WHERE kind='work'").fetchone()[0],encode(value))
        self.assertIsInstance(packet['payload']['data']['records'][0][2],str)

    def test_duplicate_import_creates_no_completed_identity_receipt(self):
        packet=self.packet(work_value(state='accepted'))
        first=self.receive(packet)
        self.assertEqual(self.receive(packet),first)
        with self.target.connect() as db:
            self.assertEqual(db.execute('SELECT kind FROM records ORDER BY kind').fetchall(),[('peers',),('work',)])

    def test_concurrent_local_change_is_not_overwritten(self):
        packet=self.packet(work_value(state='accepted'))
        self.target.execute(lambda tx: tx.put('work','task',{'local':'new'}))
        with self.assertRaises(Fault):self.receive(packet)
        self.assertEqual(self.target.execute(lambda tx:tx.get('work','task'),read_only=True),{'local':'new'})

    def test_tampered_signature_rejected(self):
        packet=self.packet(work_value(state='accepted'))
        packet['payload']['data']['records'][0][2]=encode({'state':'wrong'})
        with self.assertRaises(Fault):self.receive(packet)

    def test_other_session_head_or_recipient_rejected(self):
        packet=self.packet(work_value(state='accepted'))
        for key,value in [('session','other'),('final_seq',4)]:
            payload=copy.deepcopy(packet['payload']);payload['data'][key]=value
            with self.assertRaises(Fault):self.receive(signed(self.key_a,payload))
        payload=copy.deepcopy(packet['payload']);payload['to']='other'
        with self.assertRaises(Fault):self.receive(signed(self.key_a,payload))

    def test_high_churn_and_security_records_rejected(self):
        for kind in ('leases','actions','events','capability_usage','peers','providers','_receipt'):
            with self.assertRaises(Fault):validate_scope([[kind,'x']])

    def test_revoked_or_changed_key_rejected(self):
        packet=self.packet(work_value(state='accepted'))
        self.target.execute(lambda tx:tx.put('peers','a',{'id':'a','key':self.pub_a,'enabled':False}))
        with self.assertRaises(Fault):self.receive(packet)

    def test_close_import_allowed_after_deadline_but_not_open_import(self):
        packet=self.packet(work_value(state='accepted'))
        self.session['expires']=0
        self.receive(packet)
        self.session['state']='open'
        with self.assertRaises(Fault):self.receive(packet)
