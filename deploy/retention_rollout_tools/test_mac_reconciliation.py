"""Standard worker-adopted reconciliation and independent read-only audit tests."""
import copy
import hashlib
import json
import pathlib
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import mac_native as m
import terminal_safety as s


class AdoptedReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        (self.home / 'terminal-results').mkdir()
        self.db = sqlite3.connect(str(self.home / 'worker.sqlite'))
        self.db.executescript('CREATE TABLE actions(id TEXT,signature TEXT,state TEXT,result TEXT,resource TEXT);'
                             'CREATE TABLE reconciliations(id TEXT,previous_result TEXT,state TEXT,result TEXT,observed REAL);')
        self.result = {'reconciliation': {'quiescent': True, 'reason': 'authenticated completion'}}
        self.prior = {'evidence': {'quiescent': False, 'process_identity': {'pid': 20, 'pgid': 20}}}
        self.signature = hashlib.sha256(b'original-operation-identity').hexdigest()
        self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                        ('operation', self.signature, 'failed', json.dumps(self.result), 'guard'))
        self.db.execute('INSERT INTO reconciliations VALUES(?,?,?,?,?)',
                        ('operation', json.dumps(self.prior), 'failed', json.dumps(self.result), 10))
        self.db.commit()
        self.snap = {'boot_id': None, 'processes': {
            10: {'pid': 10, 'ppid': 1, 'pgid': 10, 'started': 1, 'state': 'S'}}}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def check(self, fresh=None):
        with patch.object(s, 'process_snapshot', side_effect=[self.snap, fresh or self.snap]) as calls:
            report = s.inspect_local(str(self.home), 'Darwin', [10])
            if report['quiescent']:
                self.assertEqual(calls.call_count, 2)
            return report

    def change(self, sql, args=()):
        self.db.execute(sql, args)
        self.db.commit()

    def test_matching_adopted_audit_allows_missing_receipt(self):
        report = self.check()
        self.assertTrue(report['quiescent'], report)
        self.assertEqual(report['adopted_reconciliations'], ['operation'])
        self.assertEqual(report['checked_receipts'], 0)

    def test_evidence_quiescence_is_also_valid(self):
        value = json.dumps({'evidence': {'quiescent': True}})
        self.change('UPDATE actions SET result=?', (value,))
        self.change('UPDATE reconciliations SET result=?', (value,))
        self.assertTrue(self.check()['quiescent'])

    def test_canonical_results_match_despite_json_formatting(self):
        self.change('UPDATE reconciliations SET result=?', (json.dumps(self.result, indent=2, sort_keys=True),))
        self.assertTrue(self.check()['quiescent'])

    def test_mismatched_audit_state_blocks(self):
        self.change("UPDATE reconciliations SET state='succeeded'")
        self.assertFalse(self.check()['quiescent'])

    def test_mismatched_audit_result_blocks(self):
        self.change('UPDATE reconciliations SET result=?', (json.dumps(dict(self.result, changed=True)),))
        self.assertFalse(self.check()['quiescent'])

    def test_invalid_action_signature_blocks(self):
        for value in ('', 'not-a-signature', 'z' * 64, None):
            self.change('UPDATE actions SET signature=?', (value,))
            self.assertFalse(self.check()['quiescent'])

    def test_missing_audit_blocks(self):
        self.change('DELETE FROM reconciliations')
        self.assertFalse(self.check()['quiescent'])

    def test_false_or_missing_explicit_quiescence_blocks(self):
        for result in ({}, {'reconciliation': {'quiescent': False}}, {'quiescent': True}):
            value = json.dumps(result)
            self.change('UPDATE actions SET result=?', (value,))
            self.change('UPDATE reconciliations SET result=?', (value,))
            self.assertFalse(self.check()['quiescent'])

    def test_unfinished_action_cannot_use_adoption(self):
        self.change("UPDATE actions SET state='uncertain'")
        self.change("UPDATE reconciliations SET state='uncertain'")
        self.assertFalse(self.check()['quiescent'])

    def test_invalid_audit_observation_blocks(self):
        for value in (None, 0, -1, 1e30):
            self.change('UPDATE reconciliations SET observed=?', (value,))
            self.assertFalse(self.check()['quiescent'])

    def test_missing_previous_result_binding_blocks(self):
        self.change("UPDATE reconciliations SET previous_result='null'")
        self.assertFalse(self.check()['quiescent'])

    def test_live_previous_execution_pid_or_group_blocks(self):
        for pid, pgid in ((20, 99), (21, 20)):
            fresh = copy.deepcopy(self.snap)
            fresh['processes'][pid] = {'pid': pid, 'ppid': 1, 'pgid': pgid, 'started': 11, 'state': 'S'}
            self.assertFalse(self.check(fresh)['quiescent'])

    def test_live_pid_in_current_quiescent_result_still_blocks(self):
        value = json.dumps({'evidence': {'quiescent': True, 'process_identity': {'pid': 30, 'pgid': 30}}})
        self.change('UPDATE actions SET result=?', (value,))
        self.change('UPDATE reconciliations SET result=?', (value,))
        self.snap['processes'][30] = {'pid': 30, 'ppid': 1, 'pgid': 30, 'started': 11, 'state': 'S'}
        self.assertFalse(self.check()['quiescent'])

    def test_unknown_descendant_or_service_group_member_blocks(self):
        for ppid, pgid in ((10, 99), (1, 10)):
            fresh = copy.deepcopy(self.snap)
            fresh['processes'][30] = {'pid': 30, 'ppid': ppid, 'pgid': pgid, 'started': 11, 'state': 'S'}
            self.assertFalse(self.check(fresh)['quiescent'])

    def test_worker_identity_change_between_snapshots_blocks(self):
        fresh = copy.deepcopy(self.snap)
        fresh['processes'][10]['started'] = 11
        self.assertFalse(self.check(fresh)['quiescent'])

    def test_valid_signature_change_during_audit_blocks(self):
        count = [0]
        def snapshot(system):
            count[0] += 1
            if count[0] == 2:
                self.change('UPDATE actions SET signature=?', ('a' * 64,))
            return self.snap
        with patch.object(s, 'process_snapshot', side_effect=snapshot):
            report = s.inspect_local(str(self.home), 'Darwin', [10])
        self.assertFalse(report['quiescent'])
        self.assertIn('signature or audit changed', str(report['blocked']))

    def test_audit_row_change_during_scan_blocks(self):
        count = [0]
        def snapshot(system):
            count[0] += 1
            if count[0] == 2:
                self.change('UPDATE reconciliations SET observed=12')
            return self.snap
        with patch.object(s, 'process_snapshot', side_effect=snapshot):
            self.assertFalse(s.inspect_local(str(self.home), 'Darwin', [10])['quiescent'])

    def test_no_ledger_or_receipt_mutations(self):
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertTrue(self.check()['quiescent'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def compact_audit(self, acknowledged=True):
        def marker(value):
            data=value.encode('utf-8')
            return json.dumps({'payload_expired': True, 'payload_bytes': len(data),
                               'payload_sha256': hashlib.sha256(data).hexdigest()})
        previous,result=self.db.execute('SELECT previous_result,result FROM reconciliations').fetchone()
        self.change('UPDATE reconciliations SET previous_result=?,result=?', (marker(previous),marker(result)))
        if acknowledged:
            self.change('CREATE TABLE result_acknowledged(id TEXT PRIMARY KEY)')
            self.change('INSERT INTO result_acknowledged VALUES(?)', ('operation',))

    def test_acknowledged_compacted_audit_is_safe_without_ledger_changes(self):
        self.compact_audit()
        before={str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        report=self.check()
        self.assertTrue(report['quiescent'], report)
        self.assertEqual(report['adopted_reconciliations'], [])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_compacted_audit_without_acknowledgement_blocks(self):
        self.compact_audit(acknowledged=False)
        self.assertFalse(self.check()['quiescent'])

    def test_acknowledged_compacted_audit_still_blocks_descendants_and_groups(self):
        self.compact_audit()
        for ppid,pgid in ((10,30),(1,10)):
            fresh=copy.deepcopy(self.snap)
            fresh['processes'][30]={'pid':30,'ppid':ppid,'pgid':pgid,'started':11,'state':'S'}
            self.assertFalse(self.check(fresh)['quiescent'])

    def test_acknowledged_compacted_audit_still_checks_available_execution_identity(self):
        self.compact_audit()
        result={'evidence':{'quiescent':True,'process_identity':{'pid':30,'pgid':30}}}
        self.change('UPDATE actions SET result=?',(json.dumps(result),))
        for pid,pgid in ((30,99),(31,30)):
            fresh=copy.deepcopy(self.snap)
            fresh['processes'][pid]={'pid':pid,'ppid':1,'pgid':pgid,'started':11,'state':'S'}
            self.assertFalse(self.check(fresh)['quiescent'])


class IndependentAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = pathlib.Path(self.tmp.name)
        self.snap = {'processes': {
            10: {'pid': 10, 'ppid': 1, 'pgid': 10, 'state': 'S', 'started': 1},
            100: {'pid': 100, 'ppid': 1, 'pgid': 100, 'state': 'S', 'started': 2}}}
        self.good = {'quiescent': True, 'blocked': [], 'checked_receipts': 600,
                     'adopted_reconciliations': ['operation']}
        self.bad = dict(self.good, quiescent=False, blocked=[{'operation': 'dispatch-child', 'reason': 'still exiting'}])

    def tearDown(self):
        self.tmp.cleanup()

    def run_audit(self, reports, times=None, job_pid=100):
        def inspect(service):
            return {'pid': job_pid if service == 'gui/501/' + m.AUDIT_LABEL else 10}
        with patch.object(m.r, 'verified'), patch.object(m.os, 'getuid', return_value=501), \
             patch.object(m.os, 'getpid', return_value=100), patch.object(m, 'inspect', side_effect=inspect), \
             patch.object(m, 'verified_worker_tree', return_value={'roots': [10]}), \
             patch.object(s, 'process_snapshot', return_value=self.snap), \
             patch.object(m, 'local_safety', side_effect=reports) as local, \
             patch.object(m.time, 'monotonic', side_effect=times or [0, 1]), \
             patch.object(m.time, 'sleep') as sleep, \
             patch.object(m, 'remote', side_effect=AssertionError('Audit must not contact offers')):
            error = None
            try:
                m.audit_local(self.bundle, 'plan-hash')
            except RuntimeError as exc:
                error = exc
            return error, local.call_count, sleep.call_count

    def test_independent_audit_waits_for_dispatch_child_then_records_proof(self):
        error, calls, sleeps = self.run_audit([self.bad, self.good])
        self.assertIsNone(error)
        self.assertEqual((calls, sleeps), (2, 1))
        report = json.loads((self.bundle / 'standalone-audit.json').read_text())
        self.assertTrue(report['outside_worker_process'])
        self.assertTrue(report['quiescent'])
        self.assertEqual(report['adopted_reconciliation_count'], 1)

    def test_audit_rejects_worker_ancestry_or_group(self):
        for field in ('ppid', 'pgid'):
            self.snap['processes'][100][field] = 10
            error, calls, _ = self.run_audit([self.good])
            self.assertIsNotNone(error)
            self.assertEqual(calls, 0)
            self.snap['processes'][100][field] = 1 if field == 'ppid' else 100

    def test_audit_requires_exact_independent_launchd_pid(self):
        error, calls, _ = self.run_audit([self.good], job_pid=999)
        self.assertIsNotNone(error)
        self.assertEqual(calls, 0)

    def test_audit_timeout_reports_bounded_blockers(self):
        bad = dict(self.bad, blocked=[{'operation': str(i), 'reason': 'blocked'} for i in range(50)])
        error, calls, sleeps = self.run_audit([bad], times=[0, 61])
        self.assertIsNotNone(error)
        self.assertEqual((calls, sleeps), (1, 0))
        report = json.loads((self.bundle / 'standalone-audit.json').read_text())
        self.assertEqual(report['blocked_count'], 50)
        self.assertEqual(len(report['blocked']), 20)

    def test_dispatch_only_bootstraps_unique_audit_agent(self):
        self.assertEqual(m.AUDIT_LABEL, 'org.biomerte.retention-audit-v11')
        with patch.object(m.r, 'verified'), patch.object(m.os, 'getuid', return_value=501), \
             patch.object(m, 'inspect', return_value=None), patch.object(m.subprocess, 'check_call') as call:
            m.dispatch_audit(self.bundle, 'plan-hash')
        plist = plistlib.loads((self.bundle / 'standalone-audit.plist').read_bytes())
        self.assertEqual(plist['Label'], m.AUDIT_LABEL)
        self.assertIn('audit-local', plist['ProgramArguments'])
        self.assertNotIn('run-terminal', plist['ProgramArguments'])
        call.assert_called_once_with(['/bin/launchctl', 'bootstrap', 'gui/501', str(self.bundle / 'standalone-audit.plist')])

    def test_dispatch_refuses_existing_audit_job(self):
        with patch.object(m.r, 'verified'), patch.object(m.os, 'getuid', return_value=501), \
             patch.object(m, 'inspect', return_value={'pid': 100}), patch.object(m.subprocess, 'check_call') as call:
            with self.assertRaises(RuntimeError):
                m.dispatch_audit(self.bundle, 'plan-hash')
            call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
