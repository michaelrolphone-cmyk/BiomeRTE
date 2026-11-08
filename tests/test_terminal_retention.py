import hashlib,json,os,sqlite3,sys,tempfile,unittest
sys.path.insert(0,os.path.join(os.path.dirname(os.path.dirname(__file__)),'providers','pi-terminal'))
import terminal_retention as r

class TerminalRetentionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.home=self.tmp.name;self.db=sqlite3.connect(':memory:');self.db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,body TEXT,result TEXT)');r.initialize(self.db)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def add(self,op,size=50):
  work=os.path.join(self.home,'jobs',hashlib.sha256(op.encode()).hexdigest());os.makedirs(work)
  paths={n:os.path.join(work,n+'.log') for n in ['stdout','stderr']}
  for p in paths.values():
   with open(p,'w') as f:f.write('x'*size)
  with self.db:self.db.execute('INSERT INTO jobs VALUES(?,?,?)',(op,json.dumps({'argv':['echo',op],'workspace':work}),json.dumps(paths)))
  out={'stdout':'hello','stderr':'','payload':'x'*size};r.cache(self.db,op,out,paths)
  return paths,out
 def test_count_and_byte_caps_preserve_uploaded_recovery(self):
  for i in range(8):self.add(str(i))
  r.trim(self.db,self.home,output_count=3,output_bytes=200)
  self.assertEqual(sum(os.path.exists(row[0]) for row in self.db.execute('SELECT stdout FROM terminal_uploads')),2)
  self.assertEqual(r.lookup(self.db,'0')[1]['stdout'],'hello')
 def test_acked_cache_bounded_but_unacknowledged_never_dropped(self):
  for i in range(8):
   paths,out=self.add(str(i));r.acknowledge(self.db,str(i),hashlib.sha256(r.encoded(out).encode()).hexdigest())
  self.add('pending',10000)
  r.trim(self.db,self.home,result_count=2,result_bytes=1000)
  self.assertEqual(self.db.execute('SELECT count(*) FROM terminal_uploads WHERE ack=1 AND result IS NOT NULL').fetchone()[0],2)
  self.assertTrue(r.lookup(self.db,'pending')[0])
  with self.assertRaisesRegex(RuntimeError,'expired'):r.lookup(self.db,'0')
  self.assertTrue(json.loads(self.db.execute("SELECT body FROM jobs WHERE id='0'").fetchone()[0])['retired'])
 def test_bad_ack_and_symlink_leave_data(self):
  paths,out=self.add('a')
  with self.assertRaises(RuntimeError):r.acknowledge(self.db,'a','bad')
  self.assertEqual(self.db.execute('SELECT ack FROM terminal_uploads').fetchone()[0],0)
  target=os.path.join(self.home,'user-file');open(target,'w').close();os.unlink(paths['stdout']);os.symlink(target,paths['stdout'])
  with self.assertRaises(RuntimeError):r.trim(self.db,self.home,output_count=0)
  self.assertTrue(os.path.exists(target))
 def test_workspace_files_and_legacy_never_scanned(self):
  paths,out=self.add('new');user=os.path.join(os.path.dirname(paths['stdout']),'user-file');open(user,'w').close()
  legacy=os.path.join(self.home,'legacy.log');open(legacy,'w').close()
  r.trim(self.db,self.home,output_count=0)
  self.assertTrue(os.path.exists(user));self.assertTrue(os.path.exists(legacy))
 def test_ack_idempotent(self):
  paths,out=self.add('a');sha=hashlib.sha256(r.encoded(out).encode()).hexdigest()
  r.acknowledge(self.db,'a',sha);r.acknowledge(self.db,'a',sha);r.trim(self.db,self.home,result_count=0);r.acknowledge(self.db,'a',sha)
if __name__=='__main__':unittest.main()
