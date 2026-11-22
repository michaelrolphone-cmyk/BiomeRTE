import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import biome_provider as adapter


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.pending = pathlib.Path(self.home.name) / 'withdrawals'
        self.cfg = dict(provider='vision', resource='camera', service='robot-vision', site='local-site', slots=1)

    def test_more_than_one_page_and_lost_ack_keep_exact_requests_and_revisions(self):
        revisions = {f'query.{i}:1': f'{i:032x}' for i in range(258)}
        calls = []
        class Remote:
            def call(self, name, args, rid):
                calls.append((name,args,rid))
                if len(calls) == 2:
                    raise TimeoutError('lost acknowledgement')
                return {'withdrawn': sorted(args['registrations']), 'superseded': []}
        remote = Remote()
        adapter.withdraw_registered_queries(remote, self.cfg, revisions, self.pending, True)
        self.assertEqual(sorted(len(x[1]['registrations']) for x in calls), [2,128,128])
        self.assertEqual(len(list(self.pending.glob('*.json'))), 1)
        adapter.withdraw_registered_queries(remote, self.cfg, {}, self.pending)
        self.assertEqual(calls[-1],calls[1])
        self.assertFalse(list(self.pending.glob('*.json')))
        self.assertEqual({k:v for _,args,_ in calls[:3] for k,v in args['registrations'].items()},revisions)

    def test_expired_receipt_rotates_only_the_fenced_request_and_keeps_it_across_restart(self):
        name = 'robot.vision.metadata:1'
        old = {name: 'a' * 32}
        seen = []
        class Retired(RuntimeError):
            status = 410
        class Remote:
            def call(self, operation, args, rid):
                seen.append((operation, args.copy(), rid))
                if len(seen) == 1:
                    raise Retired('Receipt result retired; request will not execute again')
                if len(seen) == 2:
                    raise TimeoutError('reply unavailable')
                return {'withdrawn': [], 'superseded': [name]}
        remote = Remote()
        adapter.withdraw_registered_queries(remote, self.cfg, old, self.pending)
        adapter.withdraw_registered_queries(remote, self.cfg, {}, self.pending)
        adapter.withdraw_registered_queries(remote, self.cfg, {}, self.pending)
        self.assertTrue(all(op=='cap.withdraw' and args['registrations']==old for op,args,_ in seen))
        self.assertNotEqual(seen[0][2], seen[1][2])
        self.assertEqual(seen[1][2],seen[2][2])
        self.assertFalse(list(self.pending.glob('*.json')))

    def test_unsafe_or_ambiguous_reply_never_triggers_unfenced_legacy_offer(self):
        class Missing(RuntimeError):
            status = 404
        name = 'robot.vision.metadata:1'
        for outcome in (Missing('Unknown operation'), TimeoutError('lost reply'), {'withdrawn': [], 'superseded': []}):
            with self.subTest(outcome=type(outcome).__name__):
                remote = Mock()
                if isinstance(outcome, Exception):
                    remote.call.side_effect = outcome
                else:
                    remote.call.return_value = outcome
                before = len(list(self.pending.glob('*.json')))
                adapter.withdraw_registered_queries(remote,self.cfg,{name:'a'*32},self.pending,True)
                self.assertTrue(list(self.pending.glob('*.json')))
                self.assertTrue(all(c.args[0]=='cap.withdraw' for c in remote.call.call_args_list))
                self.assertLessEqual(before,1)
        missing = Mock()
        missing.call.side_effect = Missing('Unknown operation')
        adapter.withdraw_registered_queries(missing,self.cfg,{},self.pending)
        self.assertTrue(all(c.args[0]=='cap.withdraw' for c in missing.call.call_args_list))

    def test_confirmed_legacy_authority_uses_only_exact_current_locked_queries(self):
        class Missing(RuntimeError):
            status = 404
        class Remote:
            def __init__(self): self.offline=[]
            def call(self,name,args,rid=None):
                if name == 'cap.withdraw':
                    raise Missing('Unknown operation')
                if name == 'cap.discover':
                    return {'offers':[dict(contract=args['contract'],provider='vision',resource='camera',
                                           service='another' if args['contract']=='robot.vision.status:1' else 'robot-vision',
                                           state='ready')]}
                self.offline.append((args['contract'],args['state'],args['resource'],args['provider']))
                return {}
        remote=Remote()
        adapter.withdraw_registered_queries(remote,self.cfg,{'robot.vision.metadata:1':None,'robot.vision.status:1':None},self.pending,True)
        self.assertEqual(remote.offline,[('robot.vision.metadata:1','offline','camera','vision')])
        self.assertFalse(list(self.pending.glob('*.json')))

    def test_confirmed_registration_survives_restart_with_legacy_and_new_local_rows(self):
        registry=pathlib.Path(self.home.name)/'registry.json'
        name='robot.vision.metadata:1'
        name2='robot.vision.status:1'
        adapter.write_registered(registry,self.cfg,{name2})
        revisions=adapter.read_registrations(registry,self.cfg)
        self.assertEqual(revisions,{name2:None})
        remote=Mock()
        remote.call.side_effect=lambda operation,args: {'registration':'b'*32} if operation=='cap.offer' else {}
        defined=set(revisions)
        adapter.reconcile_query_offers(remote,self.cfg,{name},set(),defined,{},registry,threading.Event(),revisions)
        self.assertEqual(adapter.read_registrations(registry,self.cfg),{name:'b'*32,name2:'b'*32})
