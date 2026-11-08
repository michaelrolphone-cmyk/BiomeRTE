"""Active failure reporting never clears or bypasses durable uncertainty."""
import json,sqlite3,tempfile,unittest
from concurrent.futures import Future
from unittest.mock import Mock
from biomerte.worker import ActionPool,Worker
class Remote:
 node='provider';site='site';realm='site'
class PoolReporting(unittest.TestCase):
 def setUp(self):
  self.pool=ActionPool.__new__(ActionPool);self.pool.limit=4;self.pool.running={};self.pool.items={};self.pool.failures={};self.pool.executor=Mock();self.pool.executor.submit.side_effect=lambda *a:Future()
 def item(self,name='old',gen=1):return dict(id=name,generation=gen,guard='g',resource='terminal')
 def failure(self,kind='fence',name='old',gen=1):
  self.pool.failures['g']=dict(kind=kind,id=name,generation=gen,guard='g',error='Fault',reason='uncertain')
 def test_canceled_fence_no_longer_degrades_health(self):
  self.failure();self.pool.dispatch([],[]);self.assertEqual({},self.pool.failures);self.assertEqual(0,self.pool.executor.submit.call_count)
 def test_replacement_fence_does_not_inherit_error(self):
  self.failure();self.pool.dispatch([],[self.item('new')]);self.assertEqual({},self.pool.failures)
 def test_replacement_action_does_not_inherit_error(self):
  self.failure();self.pool.dispatch([self.item('new')],[]);self.assertEqual({},self.pool.failures)
 def test_new_generation_same_id_does_not_inherit_error(self):
  self.failure();self.pool.dispatch([],[self.item(gen=2)]);self.assertEqual({},self.pool.failures)
 def test_changed_kind_same_id_does_not_inherit_error(self):
  self.failure();self.pool.dispatch([self.item()],[]);self.assertEqual({},self.pool.failures)
 def test_current_candidate_failure_is_retained(self):
  self.failure();self.pool.dispatch([],[self.item()]);self.assertIn('g',self.pool.failures)
 def test_corresponding_active_future_retains_error(self):
  self.failure();self.pool.running['g']=Future();self.pool.items['g']=('fence','old',1);self.pool.dispatch([],[]);self.assertIn('g',self.pool.failures)
 def test_unrelated_active_future_cannot_retain_old_error(self):
  self.failure();self.pool.running['g']=Future();self.pool.items['g']=('action','new',2);self.pool.dispatch([],[]);self.assertEqual({},self.pool.failures)
 def test_finished_failed_future_absent_from_poll_not_latched(self):
  f=Future();f.set_exception(RuntimeError('old failure'));self.pool.running['g']=f;self.pool.items['g']=('fence','old',1);self.pool.dispatch([],[]);self.assertEqual({},self.pool.failures)
 def test_finished_failed_future_still_outstanding_is_reported(self):
  f=Future();f.set_exception(RuntimeError('active failure'));self.pool.running['g']=f;self.pool.items['g']=('fence','old',1);self.pool.dispatch([],[self.item()]);self.assertEqual(1,self.pool.failures['g']['generation'])
 def test_successful_recovery_clears_error(self):
  self.failure();f=Future();f.set_result(None);self.pool.running['g']=f;self.pool.items['g']=('fence','old',1);self.pool.dispatch([],[]);self.assertEqual({},self.pool.failures)
 def test_failed_poll_never_prunes_from_unknown_queue(self):
  self.failure();w=Worker.__new__(Worker);w.cleanup_results=Mock();w.recover_result_acknowledgements=Mock();w.remote=Mock();w.remote.call.side_effect=OSError('poll failed');w.write_health=Mock()
  with self.assertRaises(OSError):w.once(self.pool)
  self.assertIn('g',self.pool.failures);self.assertEqual(0,w.write_health.call_count)
class HistoricalDebt(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.w=Worker(self.tmp.name+'/worker.sqlite',Remote(),[]);self.addCleanup(self.w.db.close)
 def seed(self,name,state='uncertain',guard='latent'):
  self.w.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',(name,'signature',state,'{"reason":"preserve"}',guard))
 def test_latent_debt_separate_from_current_guards(self):
  self.seed('old');self.seed('current',guard='active');before=list(self.w.db.execute('SELECT * FROM actions'))
  debt=self.w.historical_debt(['active']);self.assertEqual([{'id':'old','guard':'latent'}],debt['operations']);self.assertEqual(before,list(self.w.db.execute('SELECT * FROM actions')))
 def test_history_inventory_is_bounded_and_preserved(self):
  self.w.db.execute('BEGIN')
  for i in range(80):self.seed('old-%03d'%i)
  self.w.db.commit();debt=self.w.historical_debt();self.assertEqual(64,len(debt['operations']));self.assertTrue(debt['truncated']);self.assertEqual(80,self.w.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0])
 def test_completed_history_not_scanned(self):
  self.w.db.execute('BEGIN');self.w.db.executemany('INSERT INTO actions VALUES(?,?,?,?,?)',[('done-%d'%i,'s','succeeded','{}','g') for i in range(10000)]);self.w.db.commit();self.seed('old')
  instructions=[]
  def budget():instructions.append(1);return int(len(instructions)>100)
  self.w.db.set_progress_handler(budget,100)
  try:self.assertEqual(1,len(self.w.historical_debt()['operations']))
  finally:self.w.db.set_progress_handler(None,0)
  self.assertLess(len(instructions),100)
 def test_ready_with_separately_reported_history(self):
  self.seed('old');self.w.write_health([],0,self.w.historical_debt());self.assertEqual('ready',self.w.last_health['state']);self.assertEqual([],self.w.last_health['blocked']);self.assertEqual('old',self.w.last_health['historical_debt']['operations'][0]['id'])
 def test_active_failure_remains_degraded(self):
  self.seed('old');self.w.write_health([{'kind':'fence','id':'active'}],1,self.w.historical_debt());self.assertEqual('degraded',self.w.last_health['state'])
