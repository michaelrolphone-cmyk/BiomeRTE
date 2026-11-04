"""Replay-sized durability regressions; provider effects are isolated."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_delivery import Delivery
from biomerte import assistant_support as a, process
from biomerte.store import Fault, digest, encode

class Journal:
    def __init__(self):
        self.entries={};self.head=None;self.sizes=[]
    def read(self,key):
        value=self.head if key=='head' else self.entries[int(key[4:])]
        return value,str(len(self.entries))
    def append(self,expected,entry):
        size=len(encode(entry).encode())
        if size>900000:raise Fault('Test journal rejected oversized transaction',413)
        self.sizes.append(size);self.entries[entry['seq']]=copy.deepcopy(entry)
        self.head={'seq':entry['seq'],'hash':digest(entry)}
        return self.head

class TransactionReplay(Delivery):
    def test_liveness_preserves_error_branch_loop_and_future_reads(self):
        nodes={
            'read':{'type':'set','values':{'frame':{'$':'input'}},'next':'choose'},
            'choose':{'type':'choose','cases':[{'when':{'$':'route'},'to':'overwrite'}],'default':'loop'},
            'overwrite':{'type':'service','save':'step','args':{},'next':'end','on_error':'error'},
            'loop':{'type':'set','values':{'frame':{'$':'prepared'}},'next':'choose'},
            'end':{'type':'end','result':{'$':'step'}},
            'error':{'type':'end','result':{'$':'step'}}}
        live=process.assistant_live_variables(nodes)
        self.assertIn('input',live['read'])
        self.assertIn('prepared',live['choose'])
        self.assertIn('step',live['overwrite'])
        token={'at':'end','vars':{'frame':{},'step':{'keep':True},'input':{},'route':1,'prepared':{},'child_result':{}}}
        self.assertTrue(process.compact_assistant_token({'name':'assistant.test'},token,live))
        self.assertEqual(token['vars'],{'step':{'keep':True}})
        ordinary={'at':'end','vars':{'input':'keep'}}
        self.assertFalse(process.compact_assistant_token({'name':'ordinary'},ordinary,live))
        self.assertEqual(ordinary['vars'],{'input':'keep'})

    def test_replayed_saved_temporary_values_are_removed_without_losing_result(self):
        graph={'start':'done','inputs':{},'outputs':{'context':'obj','evidence_refs':'list'},'nodes':{'done':{'type':'end','result':{'$':'frame'}}}}
        self.app.call('process.publish',{'name':'assistant.replay','graph':graph},'publish-replay')
        frame={'context':{'catalog':'x'*87000},'evidence_refs':['retained']}
        def seed(tx):
            runs=[]
            for _ in range(2):
                run=process.new_run(tx,'assistant.replay',{})
                run['tokens'][0]['vars']={'frame':copy.deepcopy(frame),'input':copy.deepcopy(frame),'prepared':{'frame':copy.deepcopy(frame),'result':copy.deepcopy(frame)},'step':copy.deepcopy(frame),'child_result':copy.deepcopy(frame)}
                tx.put('runs',run['id'],run);runs.append(run['id'])
            return runs
        ids=self.execute(seed);journal=Journal();self.tx.remote=journal
        self.app.tick('site')
        for id in ids:
            run=self.app.call('process.get',{'run':id})
            self.assertEqual(run['state'],'completed');self.assertEqual(run['result'],frame)
        self.assertLess(max(journal.sizes),900000)

    def test_oversized_batch_rolls_back_and_commits_individual_activities_once(self):
        graph={'start':'done','inputs':{'value':'str'},'outputs':{'value':'str'},'nodes':{'done':{'type':'end','result':{'value':{'$':'input.value'}}}}}
        self.app.call('process.publish',{'name':'large','graph':graph},'publish-large')
        ids=self.execute(lambda tx:[process.new_run(tx,'large',{'value':'x'*510000})['id'] for _ in range(2)])
        journal=Journal();self.tx.remote=journal
        for _ in range(2):self.app.tick('site')
        self.assertEqual(len(journal.entries),2)
        for id in ids:
            run=self.app.call('process.get',{'run':id})
            self.assertEqual(run['state'],'completed');self.assertEqual(run['steps'],1)
        self.assertLess(max(journal.sizes),900000)

    def test_one_oversized_activity_is_not_silently_committed_or_marked_healthy(self):
        journal=Journal();self.tx.remote=journal
        def oversized(tx,ctx,budget):
            tx.put('oversized','value',{'data':'x'*910000});return 1
        with patch.object(process,'tick',side_effect=oversized):
            with self.assertRaisesRegex(Fault,'oversized transaction'):self.app.tick('site')
        self.assertEqual(journal.entries,{})
        self.assertIsNone(self.execute(lambda tx:tx.get('oversized','value')))

    def test_episode_reference_retains_full_intent_and_starts_only_once(self):
        self.install();self.r()
        f=self.frame();f.update(episode_id='episode',responsibility_id='r',owner_epoch=1,assignment={'id':'task'},context={'catalog':'x'*150000})
        def check(tx):
            tx.put('offers','terminal',{'contract':'host.terminal.run:1','resource':'terminal.test','state':'ready'})
            r=tx.need('assistant_responsibilities','r');r.update(episode_definition='assistant.reasoning',episode_revision=tx.need('definitions','assistant.reasoning')['rev']);tx.put('assistant_responsibilities','r',r)
            tx.put('assistant_assignment','task',{'epoch':1})
            prepared=a.before(tx,self.ctx,'dispatch','guard',f)
            intents=tx.rows('assistant_episode_intents');self.assertEqual(len(intents),1)
            self.assertEqual(intents[0]['frame']['context'],f['context'])
            self.assertLess(len(encode(prepared['frame']['proposal']['action'])),5000)
            ref={'id':'episode','intent_digest':digest(intents[0])}
            first=a.episode_start(tx,self.ctx,ref);again=a.episode_start(tx,self.ctx,ref)
            self.assertEqual(first,again);self.assertEqual(len(tx.rows('assistant_episodes')),1)
            with self.assertRaisesRegex(Fault,'digest mismatch'):a.episode_start(tx,self.ctx,dict(ref,id='changed'))
        self.execute(check)

for name in list(dir(Delivery)):
    if name.startswith('test_') and name not in TransactionReplay.__dict__:setattr(TransactionReplay,name,None)
del Delivery
if __name__=='__main__':unittest.main()
