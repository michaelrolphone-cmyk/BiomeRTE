import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.store import Fault
from biomerte.xmlprofile import compile_xml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'deploy'))
from mcp_events import EventStore, CallbackError, EVENT, public_ip, post_callback


class Delegation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = tmp.name
        initialize(tmp.name)
        self.app = Application(tmp.name)
        with open(os.path.join(ROOT,'examples','chatgpt-delegation.json')) as f:
            self.definition = json.load(f)
        self.call('routine.publish', self.definition, 'publish')
        self.secret = 'whsec_' + base64.b64encode(b's'*32).decode()
        self.params = {'name': EVENT, 'arguments': {}, 'delivery': {
            'mode':'webhook', 'url':'https://receiver.example.com/callback', 'secret':self.secret}}
        self.codes = []
        self.records = []
        self.allowed = True

    def call(self, name, args, key=''):
        return self.app.call(name,args,key or None)

    def store(self):
        return EventStore(os.path.join(self.path,'events.sqlite'),
                          lambda realm,a,n: self.app.call('delegation.feed',{'after':a,'limit':n},realm=realm),
                          lambda owner,grant: self.allowed,self.post)

    def post(self, url, headers, body):
        sig=hmac.new(base64.b64decode(self.secret[6:]),(headers['webhook-id']+'.'+headers['webhook-timestamp']+'.').encode()+body,hashlib.sha256).digest()
        self.assertIn('v1,'+base64.b64encode(sig).decode(),headers['webhook-signature'].split())
        data=json.loads(body.decode())
        self.records.append((data,headers))
        if data.get('type')=='verification':
            return 200,json.dumps({'challenge':data['challenge']}).encode()
        return (self.codes.pop(0) if self.codes else 200),b''

    def tick(self):
        for _ in range(8):
            if not self.app.tick('site'):
                break

    def start(self):
        run=self.call('process.start',{'name':self.definition['name'],'inputs':{'observation':'Pi is online.'}},'start')['id']
        self.tick()
        w=self.call('process.waits',{'run':run})[0]
        return run,w['id']

    def test_lifecycle_and_correlated_completion(self):
        st=self.store()
        cfg=st.change('events/subscribe',self.params,'owner','grant')
        self.assertEqual(st.change('events/subscribe',self.params,'owner','grant')['id'],cfg['id'])
        self.assertEqual(len(self.records),1)
        run,w=self.start()
        self.codes=[503,200]
        st.deliver()
        self.assertEqual(st.db.execute('SELECT cursor FROM subscriptions').fetchone()[0],0)
        st.close()
        self.app=Application(self.path)
        st=self.store()
        with st.db: st.db.execute('UPDATE subscriptions SET next_try=0')
        st.deliver()
        self.assertEqual(self.records[-2][0]['eventId'],self.records[-1][0]['eventId'])
        self.assertEqual(self.records[-1][0]['data']['delegation_id'],w)
        self.assertEqual(self.call('process.get',{'run':run})['state'],'running')
        self.assertEqual(self.call('delegation.get',{'id':w})['context']['observation'],'Pi is online.')
        self.call('delegation.claim',{'id':w,'claimant':'worker-1'},'claim')
        for op,args in [('process.answer',{'wait':w,'answer':{}}),('delegation.claim',{'id':w,'claimant':'worker-2'}),
                        ('delegation.complete',{'id':w,'claimant':'worker-1','result':{'bad':2}})]:
            with self.assertRaises(Fault): self.call(op,args,'bad-'+op)
        args={'id':w,'claimant':'worker-1','result':{'summary':'Online'}}
        self.call('delegation.complete',args,'answer')
        self.tick()
        self.call('delegation.complete',args,'again')
        self.assertEqual(self.call('process.get',{'run':run})['result'],{'summary':'Online'})
        self.assertEqual(self.call('delegation.feed',{})['high'],1)
        st.change('events/unsubscribe',self.params,'owner','grant')
        self.assertFalse(st.active());st.close()

    def test_filters_rotation_revocation_cancel(self):
        st=self.store()
        self.params['arguments']={'workflow':'unrelated'}
        st.change('events/subscribe',self.params,'owner','grant')
        run,w=self.start();st.deliver();self.assertEqual(len(self.records),1)
        self.params['arguments']={}
        st.change('events/subscribe',self.params,'owner','grant')
        self.secret='whsec_'+base64.b64encode(b'x'*32).decode()
        self.params['delivery']['secret']=self.secret
        st.change('events/subscribe',self.params,'owner','grant')
        st.deliver()
        self.assertEqual(len(self.records[-1][1]['webhook-signature'].split()),2)
        self.call('process.cancel',{'run':run},'cancel')
        with self.assertRaises(Fault): self.call('delegation.claim',{'id':w,'claimant':'late'},'late')
        self.call('process.start',{'name':self.definition['name'],'inputs':{'observation':'another'}},'start2')
        self.tick();self.allowed=False;num=len(self.records);st.deliver();self.assertEqual(num,len(self.records));st.close()

    def test_lease_available_delivery_is_addressed_and_signed(self):
        st = self.store()
        st.topic_feed = lambda realm, topic, after, limit: self.app.call(
            'bus.event_page', {'topic':topic, 'after':after, 'limit':limit}, realm=realm)
        self.params.update(name='lease.available', arguments={'reply_to':'my-inbox'})
        st.change('events/subscribe', self.params, 'owner', None)
        self.call('cap.acquire', {'task':'other', 'contract':'core.echo:1', 'reply_to':'other-inbox'}, 'grant-other')
        lease = self.call('cap.acquire', {'task':'mine', 'contract':'core.echo:1', 'reply_to':'my-inbox'}, 'grant-mine')
        st.deliver()
        st.deliver()
        delivered = [body for body, headers in self.records if body.get('name') == 'lease.available']
        self.assertEqual([e['data']['id'] for e in delivered], [lease['id']])
        self.assertEqual(delivered[0]['data']['realm'], 'site')
        st.close()

    def test_verification_and_terminal_delivery(self):
        st=self.store()
        st.post=lambda *args:(200,b'{"challenge":"wrong"}')
        with self.assertRaises(CallbackError):st.change('events/subscribe',self.params,'owner',None)
        self.assertFalse(st.active());st.post=self.post
        self.start();st.change('events/subscribe',self.params,'owner',None);self.codes=[413]
        st.deliver();num=len(self.records);st.deliver();self.assertEqual(num,len(self.records))
        self.assertEqual(st.db.execute('SELECT status FROM failed_events').fetchone()[0],413);st.close()

    def test_callback_destination(self):
        for address in ['127.0.0.1','169.254.169.254','10.1.1.1','100.64.0.1','224.0.0.1','::1','2002:7f00:1::1','::ffff:127.0.0.1']:
            self.assertFalse(public_ip(address),address)
        self.assertTrue(public_ip('1.1.1.1'))
        with patch('mcp_events.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),patch('mcp_events.socket.socket') as dial:
            with self.assertRaises(CallbackError):post_callback('https://rebind.example/test',{},b'')
            dial.assert_not_called()

    def test_bpmn_delegation(self):
        xml='<process xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:b="urn:biomerte:v1" id="p"><startEvent id="s"/><sequenceFlow id="a" sourceRef="s" targetRef="d"/><serviceTask id="d" name="Review" b:delegate="chatgpt"/><sequenceFlow id="b" sourceRef="d" targetRef="e"/><endEvent id="e"/></process>'
        self.assertEqual(compile_xml(xml)['nodes']['d']['type'],'delegate')
