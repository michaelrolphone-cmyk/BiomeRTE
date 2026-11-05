import copy, unittest
import test_delivery as delivery
from biomerte import assistant_support as a

class WakeQualification(unittest.TestCase):
    setUp=delivery.Delivery.setUp
    tearDown=delivery.Delivery.tearDown
    execute=delivery.Delivery.execute
    frame=delivery.Delivery.frame
    install=delivery.Delivery.install
    r=delivery.Delivery.r
    def active(self):
        self.install();self.r()
        first=self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'initial'))
        return self.execute(lambda tx:tx.need('assistant_cycle_inputs',first['cycle_id']))
    def commit(self,f,runnable=False):
        def run(tx):
            r=tx.need('assistant_responsibilities','r')
            f['expected_revision']=r['revision']
            return a.finalize(tx,f,r,runnable)
        return self.execute(run)
    def trigger(self,name):
        return self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),name))
    def pump(self):self.execute(lambda tx:a.pump(tx,self.ctx))
    def test_trigger_during_observation_is_carried_into_one_successor(self):
        f=self.active()
        self.trigger('capacity-arrived');self.trigger('work-arrived');self.trigger('capacity-arrived')
        receipt=self.commit(f,False)
        self.assertEqual(receipt['coverage']['kind'],'successor')
        self.pump();self.pump()
        rows=self.execute(lambda tx:tx.rows('assistant_cycle_inputs'))
        self.assertEqual(len(rows),2)
        next_frame=next(x for x in rows if x['cycle_id']!=f['cycle_id'])
        self.assertTrue({'capacity-arrived','work-arrived'}<=set(next_frame['trigger_ids']))
        self.assertEqual(len(next_frame['trigger_ids']),len(set(next_frame['trigger_ids'])))
        self.assertEqual(self.execute(lambda tx:tx.need('assistant_responsibilities','r'))['pending_triggers'],[])
    def test_pause_finalization_retains_triggers_until_one_resume(self):
        f=self.active();self.trigger('arrived')
        self.app.call('assistant.control',{'id':'r','command':'pause','command_id':'pause'},'pause')
        receipt=self.commit(f,True);self.assertEqual(receipt['coverage']['kind'],'pause')
        self.pump();self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_cycle_inputs'))),1)
        self.app.call('assistant.control',{'id':'r','command':'resume','command_id':'resume'},'resume')
        self.app.call('assistant.control',{'id':'r','command':'resume','command_id':'resume'},'resume-again')
        rows=self.execute(lambda tx:tx.rows('assistant_cycle_inputs'));self.assertEqual(len(rows),2)
        next_frame=next(x for x in rows if x['cycle_id']!=f['cycle_id'])
        self.assertIn('arrived',next_frame['trigger_ids'])
    def wake_position(self,position):
        f=self.active();f['event_cursor']=0;f['wait_topics']=['qualification.ready']
        if position=='before':self.execute(lambda tx:tx.emit('qualification.ready',{'ready':True}))
        if position=='inside':
            def transaction(tx):
                tx.emit('qualification.ready',{'ready':True})
                r=tx.need('assistant_responsibilities','r');f['expected_revision']=r['revision']
                return a.finalize(tx,f,r,False)
            self.execute(transaction)
        else:self.commit(f,False)
        if position=='after':self.execute(lambda tx:tx.emit('qualification.ready',{'ready':True}))
        self.pump();self.pump()
        rows=self.execute(lambda tx:tx.rows('assistant_cycle_inputs'))
        self.assertEqual(len(rows),2,position)
    def test_readiness_before_wait_commit(self):self.wake_position('before')
    def test_readiness_inside_wait_transaction(self):self.wake_position('inside')
    def test_readiness_after_wait_commit(self):self.wake_position('after')
    def test_ready_wake_stays_pending_while_paused(self):
        f=self.active();f['event_cursor']=0;f['wait_topics']=['qualification.ready'];self.commit(f,False)
        self.app.call('assistant.control',{'id':'r','command':'pause','command_id':'pause'},'pause')
        self.execute(lambda tx:tx.emit('qualification.ready',{'ready':True}))
        self.pump()
        self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_cycle_inputs'))),1)
        wakes=self.execute(lambda tx:tx.rows('assistant_wake_outbox'));self.assertEqual(len(wakes),1);self.assertTrue(wakes[0]['ready'])

if __name__=='__main__':unittest.main()
