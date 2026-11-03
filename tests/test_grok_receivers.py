import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import ssl
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
import unittest
from biomerte.app import initialize, Application
from biomerte.store import uid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'deploy'))
from grok_receivers import mutate
from mcp_events import EventStore, EVENT, post_callback


class GrokReceivers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        initialize(tmp.name)
        self.app = Application(tmp.name)
        self.path = os.path.join(tmp.name, 'subscriptions.sqlite')
        self.messages = []
        self.code = 200
        self.allowed = True
        self.st = self.open()
        self.addCleanup(lambda: self.st.close())
        self.url = 'https://api2.cursor.sh/automations/webhook/private-example'
        self.creds = {'receiver':'field-bot', 'url':self.url, 'key':'example-key', 'request_id':'new'}

    def open(self):
        return EventStore(self.path, lambda realm,a,n:self.app.call('delegation.feed',{'after':a,'limit':n},realm=realm),
                          lambda owner,grant:self.allowed, self.post,
                          lambda realm,topic,a,n:self.app.call('bus.event_page',{'topic':topic,'after':a,'limit':n},realm=realm))

    def post(self,url,headers,body):
        data=json.loads(body.decode())
        self.messages.append((url,headers,data,body))
        if data.get('type')=='verification':
            return 200,json.dumps({'challenge':data['challenge']}).encode()
        return self.code,b''

    def call(self,name,args,realm='site'):
        return self.app.call(name,args,uid(),realm)

    def start(self,target,realm='site'):
        name='delegate-'+target
        self.call('routine.publish',dict(name=name,inputs={},outputs={'answer':'str'},steps=[{'type':'delegate','to':target,'request':'Return the mode','outputs':{'answer':'str'},'save':'out'}],result={'$':'out'}),realm)
        run=self.call('process.start',{'name':name,'inputs':{}},realm)['id']
        self.app.tick(realm)
        return run

    def register(self,**extra):
        args=dict(self.creds);args.update(extra)
        return mutate(self.st,'register',args,'owner','grant')

    def test_targeted_delivery_and_completion(self):
        chat_key='whsec_'+base64.b64encode(b'c'*32).decode()
        self.st.change('events/subscribe',{'name':EVENT,'arguments':{},'delivery':{'mode':'webhook','url':'https://chat.example/cb','secret':chat_key}},'owner',None)
        self.messages=[]
        rec=self.register()
        self.assertEqual(self.messages,[])
        self.assertIsNone(rec['last_accepted_at'])
        grok=self.start('grok')
        chat=self.start('chatgpt')
        self.st.deliver()
        by_run={entry[2]['data']['run_id']:entry for entry in self.messages}
        self.assertEqual(len(self.messages),2)
        url,h,e,b=by_run[grok]
        self.assertEqual(url,self.url)
        self.assertEqual(h['Authorization'],'Bearer example-key')
        self.assertEqual(h['Idempotency-Key'],e['eventId'])
        self.assertNotIn('webhook-signature',h)
        self.assertEqual(by_run[chat][0],'https://chat.example/cb')
        self.assertNotIn('Authorization',by_run[chat][1])
        wait=e['data']['delegation_id']
        self.assertEqual(self.call('delegation.get',{'id':wait})['to'],'grok')
        self.assertEqual(self.call('process.get',{'run':grok})['state'],'running')
        self.call('delegation.claim',{'id':wait,'claimant':'grok-run'})
        self.call('delegation.complete',{'id':wait,'claimant':'grok-run','result':{'answer':'observed'}})
        for _ in range(6):
            if not self.app.tick('site'):
                break
        self.assertEqual(self.call('process.get',{'run':grok})['result'],{'answer':'observed'})
        view=mutate(self.st,'list',{},'owner','grant')
        self.assertEqual(len(view['receivers']),1)
        self.assertIsNotNone(view['receivers'][0]['last_accepted_at'])
        self.assertNotIn('private-example',json.dumps(view))
        self.assertNotIn('example-key',json.dumps(view))

    def test_retry_rotation_lifecycle_and_auth(self):
        self.register()
        self.start('grok')
        self.code=401
        self.st.deliver()
        self.st.close();self.st=self.open()
        self.st.deliver()
        self.assertEqual(len(self.messages),1)
        self.assertEqual(self.register()['cursor'],0)
        self.register(key='new-key',request_id='rotate')
        self.code=200;self.st.deliver()
        self.assertEqual(len(self.messages),2)
        self.assertEqual(self.messages[0][2]['eventId'],self.messages[1][2]['eventId'])
        self.assertEqual(self.messages[1][1]['Authorization'],'Bearer new-key')
        self.assertEqual(mutate(self.st,'list',{},'owner','other-grant')['receivers'],[])
        with self.assertRaises(ValueError):
            mutate(self.st,'unregister',{'receiver':'field-bot','request_id':'alien'},'owner','other-grant')
        rm={'receiver':'field-bot','request_id':'remove'}
        self.assertTrue(mutate(self.st,'unregister',rm,'owner','grant')['removed'])
        self.register()
        self.assertEqual(mutate(self.st,'list',{},'owner','grant')['receivers'],[])
        self.assertTrue(mutate(self.st,'unregister',rm,'owner','grant')['removed'])

    def test_explicit_topic_and_signature_mode(self):
        key='whsec_'+base64.b64encode(b'g'*32).decode()
        self.call('bus.publish',{'topic':'user.sensor','payload':{'old':True}})
        self.register(auth_mode='standard_webhooks',key=key,topic='user.sensor')
        self.call('bus.publish',{'topic':'user.ignore','payload':{}})
        self.call('bus.publish',{'topic':'user.sensor','payload':{'reading':13}})
        self.code=202;self.st.deliver()
        self.assertEqual(len(self.messages),1)
        _,h,e,b=self.messages[0]
        self.assertEqual(e['name'],'user.sensor')
        self.assertEqual(e['data'],{'reading':13})
        signed=(h['webhook-id']+'.'+h['webhook-timestamp']+'.').encode()+b
        self.assertEqual(h['webhook-signature'],'v1,'+base64.b64encode(hmac.new(b'g'*32,signed,hashlib.sha256).digest()).decode())
        self.call('bus.publish',{'topic':'user.sensor','payload':{'large':'x'*10000}})
        self.st.deliver()
        payload=self.messages[-1][2]
        self.assertEqual(payload['data'],{'payload_omitted':True})
        self.assertEqual(len(self.call('bus.event_get',{'sequence':int(payload['cursor'])})['payload']['large']),10000)
        self.allowed=False
        self.call('bus.publish',{'topic':'user.sensor','payload':{'new':True}})
        self.st.deliver()
        self.assertEqual(len(self.messages),2)
        self.assertEqual(mutate(self.st,'list',{},'owner','grant')['receivers'],[])

    def test_reject_bad_registration_and_filter_realms(self):
        for bad in [dict(url='http://example.com/x'),dict(url='https://api2.cursor.sh'),dict(key='key\nInjected: yes'),dict(topic='*'),dict(from_cursor=-1)]:
            with self.assertRaises(ValueError): self.register(**bad)
        self.register(realm='local',workflow='delegate-grok')
        self.start('grok','site')
        self.st.deliver()
        self.assertEqual(self.messages,[])
        self.start('grok','local');self.st.deliver()
        self.assertEqual(self.messages[0][2]['realm'],'local')

    def test_real_https_post_enforces_bearer_and_receives_json(self):
        # The receiver and trust root are confined to the fixture. Only its DNS address
        # and test CA are substituted; the production HTTP, TLS, and SNI paths run.
        received = []
        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.headers.get('Authorization') != 'Bearer local-test-secret':
                    self.send_error(401)
                    return
                received.append((self.path, self.headers['Idempotency-Key'], json.loads(body.decode())))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = os.path.join(tmp,'cert.pem'), os.path.join(tmp,'key.pem')
            subprocess.check_call(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                                   '-subj','/CN=grok-fixture.test','-keyout',key,'-out',cert],
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            server = HTTPServer(('127.0.0.1',0), Receiver)
            tls = ssl.SSLContext(getattr(ssl,'PROTOCOL_TLS_SERVER',ssl.PROTOCOL_SSLv23))
            tls.load_cert_chain(cert,key)
            server.socket = tls.wrap_socket(server.socket,server_side=True)
            client = ssl.create_default_context(cafile=cert)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                with patch('mcp_events.socket.getaddrinfo',return_value=[(2,1,6,'',server.server_address)]), \
                        patch('mcp_events.public_ip',side_effect=lambda value:value=='127.0.0.1'), \
                        patch('mcp_events.ssl.create_default_context',return_value=client):
                    self.st.post = post_callback
                    self.register(key='local-test-secret', url='https://grok-fixture.test/automations/webhook/test-receiver')
                    self.start('grok')
                    self.st.deliver()
                self.assertEqual(len(received),1)
                path, delivery, event = received[0]
                self.assertEqual(path,'/automations/webhook/test-receiver')
                self.assertEqual(delivery,event['eventId'])
                self.assertEqual(event['name'],EVENT)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
