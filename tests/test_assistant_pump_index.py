"""Pending assistant delivery must not deserialize settled historical outcomes."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from biomerte import assistant_support as assistant
from biomerte import store as storage
from biomerte.store import Store, Records, encode

class AssistantPumpIndexTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.store = Store(self.home.name, 'site')
        self.ctx = SimpleNamespace()
        self.store.execute(lambda tx:tx.put('assistant_config','current',{'configured':True}))

    def tearDown(self):
        self.home.cleanup()

    def seed(self, tx, key, state='completed', checked=None, dispatch=False):
        row = {'id':key,'run_id':'run-'+key,'responsibility_id':'owner','state':state,
               'result':{'historical':'large-outcome-'*10000}}
        if checked is not None:
            row['settlement_checked'] = checked
        tx.put('assistant_background',key,row)
        tx.put('runs','run-'+key,{'id':'run-'+key,'state':'completed',
               'name':'assistant.dispatch' if dispatch else 'assistant.reasoning',
               'result':{'frame':{},'disposition':'completed','result_code':'ok'}})
        return row

    def test_legacy_terminal_dispatch_reconciles_once_and_keeps_outcome(self):
        expected = self.store.execute(lambda tx:self.seed(tx,'legacy',dispatch=True))
        with patch.object(assistant,'reconcile_dispatch',wraps=assistant.reconcile_dispatch) as reconcile:
            self.store.execute(lambda tx:assistant.pump(tx,self.ctx))
            self.store.execute(lambda tx:assistant.pump(tx,self.ctx))
        self.assertEqual(reconcile.call_count,1)
        saved = self.store.execute(lambda tx:tx.need('assistant_background','legacy'),read_only=True)
        self.assertTrue(saved['dispatch_reconciled'])
        self.assertTrue(saved['settlement_checked'])
        self.assertEqual(saved['result'],expected['result'])

    def test_steady_pump_never_decodes_settled_historical_payloads(self):
        self.store.execute(lambda tx:self.seed(tx,'settled',checked=True,dispatch=True))
        self.store.execute(lambda tx:tx.put('assistant_learning_outbox','learned',
                           {'id':'learned','delivered':True,'outcome':{'private':'large-outcome-'*10000}}))
        original = storage.json.loads
        def decode(raw,*args,**kwargs):
            if 'large-outcome-' in raw:
                raise AssertionError('Historical outcome was decoded')
            return original(raw,*args,**kwargs)
        with patch.object(storage.json,'loads',side_effect=decode):
            self.store.execute(lambda tx:assistant.pump(tx,self.ctx))

    def test_running_row_with_old_checked_marker_still_settles(self):
        self.store.execute(lambda tx:self.seed(tx,'active',state='running',checked=True))
        self.store.execute(lambda tx:assistant.pump(tx,self.ctx))
        row = self.store.execute(lambda tx:tx.need('assistant_background','active'),read_only=True)
        self.assertEqual(row['state'],'completed')
        self.assertTrue(row['settlement_checked'])

    def test_missing_learning_delivery_flag_is_still_delivered_once(self):
        self.store.execute(lambda tx:tx.put('assistant_learning_outbox','pending',
                          {'id':'pending','outcome':{'frame':{'safe':True},'disposition':'completed'}}))
        with patch.object(assistant.process,'new_run',return_value={'id':'learning-run'}) as launch:
            self.store.execute(lambda tx:assistant.pump(tx,self.ctx))
            self.store.execute(lambda tx:assistant.pump(tx,self.ctx))
        self.assertEqual(launch.call_count,1)
        row = self.store.execute(lambda tx:tx.need('assistant_learning_outbox','pending'),read_only=True)
        self.assertTrue(row['delivered'])
        self.assertEqual(row['run_id'],'learning-run')

    def test_terminal_episode_enters_learning_and_is_not_replayed(self):
        def seed(tx):
            tx.put('assistant_episodes','episode',{'id':'episode','run_id':'episode-run',
                   'responsibility_id':'owner','state':'running'})
            tx.put('runs','episode-run',{'id':'episode-run','state':'failed','name':'assistant.episode',
                   'result':{'disposition':'faulted','frame':{'evidence_refs':[]}}})
        self.store.execute(seed)
        with patch.object(assistant.process,'new_run',return_value={'id':'learn-episode'}) as launch:
            self.store.execute(lambda tx:assistant.pump(tx,self.ctx))
            self.store.execute(lambda tx:assistant.pump(tx,self.ctx))
        self.assertEqual(launch.call_count,1)
        row = self.store.execute(lambda tx:tx.need('assistant_episodes','episode'),read_only=True)
        self.assertEqual(row['state'],'failed')

    def test_index_upgrade_backfills_legacy_missing_marker_without_record_edits(self):
        original = self.store.execute(lambda tx:self.seed(tx,'upgrade'))
        db = self.store.connect()
        try:
            db.execute('DELETE FROM record_lookup_version WHERE version=2')
            db.execute("DELETE FROM record_lookup WHERE kind='assistant_background'")
            db.execute("DELETE FROM record_lookup_dirty WHERE kind='assistant_background'")
        finally:
            db.close()
        reopened = Store(self.home.name,'site')
        selected = reopened.execute(lambda tx:tx.matching('assistant_background','settlement_checked',(False,)),read_only=True)
        self.assertEqual([x['id'] for x in selected],['upgrade'])
        self.assertEqual(selected[0],original)

    def test_dirty_legacy_sql_write_uses_current_marker_before_reindex(self):
        self.store.execute(lambda tx:self.seed(tx,'dirty',checked=True))
        db = self.store.connect()
        try:
            value = self.store.execute(lambda tx:tx.need('assistant_background','dirty'),read_only=True)
            value.pop('settlement_checked')
            db.execute("UPDATE records SET value=? WHERE kind='assistant_background' AND id='dirty'",(encode(value),))
        finally:
            db.close()
        selected = self.store.execute(lambda tx:tx.matching('assistant_background','settlement_checked',(False,)),read_only=True)
        self.assertEqual([x['id'] for x in selected],['dirty'])

if __name__ == '__main__':
    unittest.main()
