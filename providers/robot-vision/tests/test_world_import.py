import tempfile
import unittest
from world_import import WorldImportQueue

class Store:
    def latest_backend_checkpoint(self, mid):
        return dict(availability='available',map_id=mid,checkpoint_id='cp')
class Atlas:
    def __init__(self):self.calls=[]
    def update(self, cp):self.calls.append(cp)
    def status(self):return dict(world_id='world',revision=10,submaps=[dict(map_id='imported',alignment='unlocalized')])
class Imports(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.q=WorldImportQueue(self.temp.name);self.atlas=Atlas()
    def tearDown(self):self.temp.cleanup()
    def test_idempotent_and_persistent(self):
        self.q.submit('job','imported','cp');q=WorldImportQueue(self.temp.name)
        self.assertEqual(q.get('job')['state'],'queued')
        result=q.drain_one(Store(),self.atlas,'active')
        self.assertEqual(result['state'],'completed');self.assertEqual(result['receipt']['alignment'],'unlocalized')
        self.assertEqual(self.q.submit('job','imported','cp'),result)
        self.assertIsNone(q.drain_one(Store(),self.atlas,'active'));self.assertEqual(len(self.atlas.calls),1)
        with self.assertRaises(ValueError):q.submit('job','different','cp')
    def test_active_or_changed_checkpoint_cannot_publish(self):
        for mid,cp in [('active','cp'),('imported','changed')]:
            self.q.submit(mid,mid,cp)
            result=self.q.drain_one(Store(),self.atlas,'active')
            self.assertEqual(result['state'],'failed')
        self.assertEqual(self.atlas.calls,[])
    def test_interrupted_owner_resumes_same_reference(self):
        self.q.submit('job','imported','cp')
        with self.q._connect() as db:db.execute("UPDATE imports SET state='integrating'")
        result=WorldImportQueue(self.temp.name).drain_one(Store(),self.atlas,'active')
        self.assertEqual(result['state'],'completed');self.assertEqual(self.atlas.calls[0]['map_id'],'imported')
    def test_bad_identifiers_rejected(self):
        for value in [None,'','x'*257,123]:
            with self.assertRaises(ValueError):self.q.submit(value,'imported','cp')
