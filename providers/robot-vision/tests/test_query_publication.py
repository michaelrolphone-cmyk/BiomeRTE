import json
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from query_publication import QueryPublications


class LookupFault(RuntimeError):
    status = 404


class Authority:
    def __init__(self, rows):
        self.rows, self.calls, self.serial = rows, [], 0
        self.lose_ack = False
        self.fail_before = False

    def call(self, operation, args, rid=None):
        self.calls.append((operation,args,rid))
        if operation == 'cap.registrations':
            return {'registrations':[dict(contract=n,**self.rows.get(n,{'status':'missing'})) for n in args['contracts']]}
        if operation == 'cap.withdraw':
            for n,rev in args['registrations'].items():
                row=self.rows[n]
                if row['registration']!=rev:
                    raise AssertionError('unexpected stale fixture')
                row.update(state='offline',withdrawn_from=rev,registration='retired-'+str(rev))
            return {'withdrawn':list(args['registrations']),'superseded':[]}
        if operation == 'cap.define':
            return {}
        if operation == 'cap.offer':
            n=args['contract']
            if args.get('expected_absent') and n in self.rows:
                raise RuntimeError('concurrent create')
            if 'expected_registration' in args and (self.rows[n].get('registration') or '') != args['expected_registration']:
                raise RuntimeError('later owner')
            self.serial+=1
            self.rows[n]=dict(status='found',registration=str(self.serial),state=args['state'],published_by=args['publication_id'])
            return dict(contract=n,**self.rows[n])
        if self.fail_before:
            self.fail_before=False
            raise TimeoutError('no response')
        result=[]
        for e in args['offers']:
            n=e['offer']['contract'];row=self.rows[n]
            if e['expected_registration']!=row['registration']:
                result.append(dict(contract=n,status='superseded'));continue
            if row['state']==e['offer']['state']:
                result.append(dict(contract=n,state=row['state'],status='unchanged',registration=row['registration']));continue
            self.serial+=1
            row.update(registration=str(self.serial),published_by=args['publication_id'],state=e['offer']['state'])
            result.append(dict(contract=n,status='published',state=row['state'],registration=row['registration']))
        if self.lose_ack:
            self.lose_ack=False
            raise TimeoutError('response lost after durable completion')
        return {'offers':result}


class BulkQueries(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.cfg=dict(state=temp.name,provider='camera',resource='rover',service='robot-vision')
        self.stop=threading.Event()
        self.defs={f'q{i}:1':dict(name=f'q{i}',version=1,inputs={},outputs={}) for i in range(47)}
        self.revs={n:'old-'+n for n in self.defs}
        self.states=dict.fromkeys(self.defs,'ready')
        self.advertised={}
        self.remote=Authority({n:dict(status='found',registration='offline-'+n,state='offline',withdrawn_from=r)
                               for n,r in self.revs.items()})
        self.p=self.publisher()

    def publisher(self):
        def offer(cfg,name,state):return dict(provider=cfg['provider'],resource='rover',service='robot-vision',
                                               contract=name,state=state,capacity=4,usage={'after':'check result'})
        return QueryPublications(self.remote,self.cfg,self.defs,offer,lambda path,v:path.write_text(json.dumps(v)),str)

    def remember(self,name,state,revision):
        self.advertised[name]=state
        if revision:self.revs[name]=revision

    def update(self,publisher=None):
        return (publisher or self.p).reconcile(self.states,self.revs,self.advertised,self.remember,self.stop,
                                               lambda revs:None)

    def test_47_known_offline_queries_restore_in_three_pages_and_warm_ticks_do_no_registry_io(self):
        self.assertFalse(self.update())
        self.assertEqual(self.states,self.advertised)
        self.assertEqual([c[0] for c in self.remote.calls],['cap.registrations','cap.publish']*3)
        pages=[args for op,args,_ in self.remote.calls if op=='cap.publish']
        self.assertEqual([len(p['offers']) for p in pages],[16,16,15])
        self.assertEqual({e['offer']['contract'] for p in pages for e in p['offers']},set(self.states))
        self.assertTrue(all(e['definition']==self.defs[e['offer']['contract']] and
                            e['offer']['usage']=={'after':'check result'} for p in pages for e in p['offers']))
        self.remote.calls.clear()
        self.assertFalse(self.update())
        self.assertEqual(self.remote.calls,[])

    def test_lost_success_reconciles_origin_after_reboot_even_if_native_health_changed(self):
        self.states=dict(list(self.states.items())[:3]);self.remote.lose_ack=True
        self.assertTrue(self.update())
        self.assertEqual(len(list(self.p.directory.glob('*.json'))),1)
        first=next(iter(self.states));self.states[first]='offline'
        self.update(self.publisher())
        self.assertEqual(self.remote.rows[first]['state'],'offline')
        self.assertEqual(self.states,self.advertised)
        self.assertFalse(list(self.p.directory.glob('*.json')))
        self.assertEqual(len([c for c in self.remote.calls if c[0]=='cap.publish']),1)
        self.assertEqual(len([c for c in self.remote.calls if c[0]=='cap.withdraw']),1)

    def test_superseded_and_preexisting_warm_rows_are_not_claimed_or_replaced(self):
        a,b,c=list(self.states)[:3];self.states={a:'ready',b:'ready',c:'ready'}
        self.remote.rows[a]=dict(status='found',registration='successor',state='ready')
        self.revs[b]=None;self.remote.rows[b]=dict(status='found',registration='independent',state='ready')
        self.remote.rows[c]={'status':'superseded'}
        self.update()
        self.assertEqual([e['offer']['contract'] for op,args,_ in self.remote.calls if op=='cap.publish'
                          for e in args['offers']],[])
        self.assertEqual(self.remote.rows[a]['registration'],'successor')
        self.assertIsNone(self.revs[b])
        self.assertEqual(self.advertised,{a:'ready',b:'ready'})

    def test_stop_fences_a_pending_restore_even_if_the_old_offline_row_is_still_visible(self):
        self.states=dict(list(self.states.items())[:2]);self.remote.fail_before=True
        self.assertTrue(self.update())
        retired=[]
        self.p.stop(self.remember,retired.append)
        self.assertEqual(retired,[{n:'offline-'+n for n in self.states}])
        self.assertFalse(list(self.p.directory.glob('*.json')))

    def test_explicit_legacy_protocol_falls_back_once(self):
        original=self.remote.call
        def old(name,args,rid=None):
            if name=='cap.registrations':
                raise LookupFault('Unknown operation')
            return original(name,args,rid)
        self.remote.call=old
        self.assertIsNone(self.update())
        self.assertTrue(self.p.legacy)
