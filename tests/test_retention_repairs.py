import unittest
from tests import test_retention_selection as selection_tests
from biomerte import retention_selection
from biomerte.store import digest

class CompletedRepairTests(selection_tests.HistoricalSelectionTests):
    # Reuse only isolated fixture helpers, not the unrelated inherited cases.
    def setUp(self):
        super().setUp()
        self.review = {'schema': 'completed_repair_v1', 'installed_reader_evidence': 'fixture/no-readers',
                       'canonical_schema_evidence': 'fixture/native-schema'}
        self.root = {'id': 'purge', 'realm': 'site', 'completed_at': '2020-01-01T00:00:00+00:00',
                     'counts': {'guards': 1}, 'journal_mode': 'one_primary_record_per_transaction',
                     'native_mac_quiescent': False, 'remote_native_quiescence': 'unresolved elsewhere'}
        self.put('_operator_purges', 'purge', self.root)
        self.before = {'purge_id': 'purge', 'realm': 'site', 'kind': 'guards', 'key': 'resource',
                       'path': '/not-read/old-backup', 'sha256': 'b' * 64, 'bytes': 100}
        self.put('_operator_purge_before', 'a' * 64, self.before)
        self.put('_operator_purge_units', 'a' * 64, dict({k: v for k, v in self.before.items() if k in ('purge_id','realm','kind','key')}, counts={'guards': 1}))
    def repair_selection(self):
        return self.select(now=2000000000, seconds=86400, keep=1024, historical=False, repair_review=self.review)
    def test_completed_purge_diagnostics_expire_without_touching_current_guard(self):
        guard = {'generation': 17, 'holder': 'active-lease', 'operator_purge': 'purge'}
        self.put('guards', 'resource', guard)
        result = self.repair_selection()
        omitted = {(x['kind'],x['id']) for x in result['omitted']}
        self.assertEqual(omitted, {('_operator_purges','purge'),('_operator_purge_before','a'*64),('_operator_purge_units','a'*64)})
        self.assertEqual(result['record_updates'], [{'kind':'guards','id':'resource','before_sha256':digest(guard),'remove_fields':['operator_purge']}])
        self.assertEqual(self.contents()['guards','resource'],guard)
    def test_real_nested_dependency_protects_root_but_diagnostic_annotation_expires(self):
        self.put('work', 'task', {'state':'open','dependency':{'repair':'purge'}})
        self.put('guards', 'resource', {'generation':1,'holder':'lease','operator_purge':'purge'})
        result = self.repair_selection()
        self.assertNotIn(('_operator_purges','purge'), {(x['kind'],x['id']) for x in result['omitted']})
        self.assertEqual([x['remove_fields'] for x in result['record_updates']], [['operator_purge']])
    def test_unfamiliar_schema_and_missing_reader_review_are_not_inferred(self):
        self.root['pending_operation'] = 'actual-unresolved'
        self.put('_operator_purges','purge',self.root)
        self.assertEqual(self.repair_selection()['omitted'],[])
        self.root.pop('pending_operation');self.put('_operator_purges','purge',self.root)
        self.assertEqual(self.select(now=2000000000, historical=False)['omitted'],[])
    def test_unfinished_or_recent_purge_is_retained(self):
        for stamp in ('unknown','2033-05-18T03:33:20+00:00'):
            self.root['completed_at'] = stamp;self.put('_operator_purges','purge',self.root)
            self.assertEqual(self.repair_selection()['omitted'],[])
    def test_pointer_referenced_by_current_work_is_preserved(self):
        self.put('work','task',{'state':'open','recovery_pointer':'a'*64})
        self.assertEqual(self.repair_selection()['omitted'],[])
    def test_unknown_recovery_receipt_stays_unclassified(self):
        self.put('assistant_recovery_receipts','unknown',{'id':'unknown','pending':True})
        result=self.repair_selection()
        self.assertNotIn(('assistant_recovery_receipts','unknown'),{(x['kind'],x['id']) for x in result['omitted']})

    def test_recovery_requires_exact_event_witness_completed_targets_and_existing_window(self):
        receipt={'id':'repair','runs':['finished']}
        self.put('runs','finished',{'id':'finished','state':'completed'})
        self.put('assistant_recovery_receipts','repair',receipt)
        self.put('events','00000000000000000001',{'seq':1,'topic':'assistant.recovery.resumed','payload':receipt},completed_order=1)
        self.put('events','00000000000000000009',{'seq':9,'topic':'new','payload':{}},completed_order=9)
        result=self.select(now=100,seconds=86400,keep=1,historical=False,repair_review=self.review)
        self.assertIn(('assistant_recovery_receipts','repair'),{(x['kind'],x['id']) for x in result['omitted']})
        self.put('runs','finished',{'id':'finished','state':'running'})
        result=self.select(now=100,seconds=86400,keep=1,historical=False,repair_review=self.review)
        self.assertNotIn(('assistant_recovery_receipts','repair'),{(x['kind'],x['id']) for x in result['omitted']})

    def test_receipt_name_or_mismatched_resume_payload_does_not_prove_age(self):
        self.put('runs','finished',{'id':'finished','state':'completed'})
        self.put('assistant_recovery_receipts','repair-19000101',{'id':'repair-19000101','runs':['finished']})
        self.put('events','00000000000000000001',{'seq':1,'topic':'assistant.recovery.resumed','payload':{'id':'repair-19000101'}},completed_order=1)
        result=self.select(now=2000000000,seconds=1,keep=0,historical=False,repair_review=self.review)
        self.assertNotIn(('assistant_recovery_receipts','repair-19000101'),{(x['kind'],x['id']) for x in result['omitted']})

    def test_unverified_nested_run_schema_never_becomes_a_repair_exemption(self):
        self.put('runs','finished',{'id':'finished','state':'completed'})
        for value in ({'id':'finished'}, {'id':'finished','pending':True,'retry_authority':{'operation':'unknown'}}):
            receipt={'id':'repair','runs':[value]}
            self.put('assistant_recovery_receipts','repair',receipt)
            self.put('events','00000000000000000001',{'seq':1,'topic':'assistant.recovery.resumed','payload':receipt},completed_order=1)
            result=self.select(now=2000000000,seconds=1,keep=0,historical=False,repair_review=self.review)
            self.assertNotIn(('assistant_recovery_receipts','repair'),{(x['kind'],x['id']) for x in result['omitted']})
            self.assertTrue(any(x['id']=='repair' for x in result['unclassified_repair']))

    def test_canonical_witness_distinguishes_boolean_and_number(self):
        from biomerte.store import digest
        run={'id':'finished','state':'completed','steps':1}
        item={'id':'finished','before':'completed','after':'completed','steps':1,'saved_state_sha256':digest(run)}
        left={'id':'repair','runs':[item]}
        right={'id':'repair','runs':[dict(item,steps=True)]}
        self.assertEqual(left,right)
        self.assertNotEqual(digest(left),digest(right))
        self.put('runs','finished',run)
        self.put('assistant_recovery_receipts','repair',left)
        self.put('events','00000000000000000001',{'seq':1,'topic':'assistant.recovery.resumed','payload':right},completed_order=1)
        result=self.select(now=2000000000,keep=0,historical=False,repair_review=self.review)
        self.assertFalse(any(x['kind']=='assistant_recovery_receipts' for x in result['omitted']))

    def test_exact_native_nested_schema_requires_current_run_hash_and_no_extra_fields(self):
        run={'id':'finished','state':'completed','steps':17}
        item={'id':'finished','before':'completed','after':'completed','steps':17,'saved_state_sha256':digest(run)}
        self.put('runs','finished',run)
        for variant, allowed in ((item,True), (dict(item,pending=True),False),
                                 (dict(item,saved_state_sha256='0'*64),False), (dict(item,steps=True),False)):
            receipt={'id':'repair','runs':[variant]}
            self.put('assistant_recovery_receipts','repair',receipt)
            self.put('events','00000000000000000001',{'seq':1,'topic':'assistant.recovery.resumed','payload':receipt},completed_order=1)
            result=self.select(now=2000000000,keep=0,historical=False,repair_review=self.review)
            self.assertEqual(allowed,any(x['kind']=='assistant_recovery_receipts' for x in result['omitted']))

# Avoid running the base suite twice; fixture methods stay available.
for name in list(selection_tests.HistoricalSelectionTests.__dict__):
    if name.startswith('test_') and name not in CompletedRepairTests.__dict__:
        setattr(CompletedRepairTests,name,None)
