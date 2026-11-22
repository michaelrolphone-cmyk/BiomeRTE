"""The enrolled native registration protocol against the actual signed controller."""
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import biome_provider as adapter
from query_publication import QueryPublications
from biomerte.app import Application, initialize
from biomerte.identity import fingerprint, generate, sign
from biomerte.store import uid


class SignedQueryPublication(unittest.TestCase):
    def test_47_real_vision_schemas_and_offers_recover_in_six_signed_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            state=root/'state';state.mkdir()
            home=root/'authority';initialize(str(home),'qualification')
            app=Application(str(home))
            private=str(root/'provider.key')
            public=generate(private);actor=fingerprint(public)
            definitions=dict(list(adapter.CONTRACTS.items())[:47])
            self.assertEqual(len(definitions),47)
            cfg=dict(state=str(state),provider=actor,resource='vision',site='qualification',slots=4)
            permissions=['cap.define','cap.offer','resource:vision',*('offer:'+name for name in definitions)]
            app.call('peer.trust',dict(public_key=public,label='publisher',scopes=permissions),uid())
            old={}
            for name,spec in definitions.items():
                app.call('cap.define',spec,uid())
                old[name]=app.call('cap.offer',adapter.offer(cfg,name,'ready'),uid())['registration']
            app.call('cap.withdraw',dict(provider=actor,resource='vision',service='robot-vision',registrations=old),uid())
            calls=[]
            class Remote:
                def call(self,name,args,rid=None):
                    rid=rid or uid()
                    calls.append(name)
                    body=dict(site=cfg['site'],realm='site',operation=name,arguments=args,id=rid)
                    return app.call(name,args,rid,auth=dict(actor=actor,signature=sign(private,body)))
            worker=QueryPublications(Remote(),cfg,definitions,adapter.offer,adapter.write_registry_json,adapter.retry_diagnostic)
            local_revisions=old.copy()
            states=dict.fromkeys(definitions,'ready');observed={}
            def remember(name,state,revision):
                observed[name]=state
                if revision is not None:
                    local_revisions[name]=revision
            def stop_unused(revisions):self.fail('No ambiguous retirement in fixture')
            for _ in range(2):
                self.assertFalse(worker.reconcile(states,local_revisions,observed,remember,threading.Event(),stop_unused))
            self.assertEqual(calls,['cap.registrations','cap.publish']*3)
            self.assertEqual(observed,states)
            self.assertTrue(all(local_revisions[n]!=old[n] for n in states))
            confirmed={v['contract']:v for v in app.call('cap.discover',{},uid())['offers'] if v['provider']==actor}
            for name in definitions:
                self.assertEqual((confirmed[name]['state'],confirmed[name]['registration']),('ready',local_revisions[name]))
                self.assertEqual(confirmed[name]['capacity'],4)
