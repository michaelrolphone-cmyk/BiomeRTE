"""Native adapter lifecycle tests; no RF capture or simulated provider."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

class AdapterTest(unittest.TestCase):
    def test_persistence_and_operation_identity(self):
        with tempfile.TemporaryDirectory() as root:
            home=os.path.join(root,'service')
            state=os.path.join(root,'state')
            os.mkdir(state)
            with open(os.path.join(state,'monitor.json'),'w') as f:
                json.dump({'enabled':False,'revision':0},f)
            server=os.path.join(os.path.dirname(__file__),'server.py')
            p=subprocess.Popen([sys.executable,server,'serve',home,state],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.time()+10
                while not os.path.exists(os.path.join(home,'service.sock')):
                    if p.poll() is not None:
                        self.fail(p.stderr.read().decode())
                    if time.time()>deadline:self.fail('Daemon socket did not appear')
                    time.sleep(.02)
                def call(operation,inputs):
                    child=subprocess.Popen([sys.executable,server,'call',home],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    out,err=child.communicate(json.dumps({'operation':operation,'inputs':inputs}).encode())
                    self.assertEqual(child.returncode,0,err.decode())
                    return json.loads(out.decode())
                mark={'action':'mark','label':'test_start','note':'isolated adapter test'}
                first=call('mark-1',mark)
                self.assertTrue(first['ok'])
                self.assertEqual(call('mark-1',mark),first)
                self.assertFalse(call('mark-1',dict(mark,label='test_end'))['ok'])
                rows=call('events-1',{'action':'events'})['data']['events']
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['note'],'isolated adapter test')
                self.assertTrue(call('status-1',{'action':'status'})['data']['monitor_thread_alive'])
                self.assertFalse(call('bad-1',{'action':'unknown'})['ok'])
            finally:
                p.terminate()
                p.communicate()

if __name__=='__main__':unittest.main()
