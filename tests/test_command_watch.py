import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from biomerte.command_watch import capture, communicate


class CommandWatch(unittest.TestCase):
    def test_disk_tail_is_bounded_and_reports_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            child=subprocess.Popen([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'x'*2000000)"],stdout=subprocess.PIPE)
            path=os.path.join(directory,'tail')
            counts={}
            thread=capture(child.stdout,path,counts,'stdout',65536)
            child.wait()
            thread.join()
            self.assertEqual(65536,os.path.getsize(path))
            self.assertEqual(2000000,counts['stdout']['bytes_seen'])
            self.assertTrue(counts['stdout']['truncated'])

    def test_output_progress_keeps_long_process_alive_and_explicit_kill_stops_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,'progress')
            calls=[]
            class Remote:
                node='provider'
                def call(self,name,args):
                    calls.append(name)
                    return {'state':'claimed','kill_requested':calls.count('cap.command.state')>=4}
            worker=types.SimpleNamespace(remote=Remote())
            code="""import sys,json,time,os
r=json.load(sys.stdin);p=r['progress_path'];i=0
while not os.path.exists(p+'.stop'):
 i+=1
 t=p+'.tmp'
 with open(t,'w') as f: json.dump({'signature':str(i),'output':{'stdout':str(i)}},f)
 os.replace(t,p)
 time.sleep(.03)
print(json.dumps({'stopped':True}))
"""
            child=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            out,err=communicate(worker,child,{'progress_path':path},{'id':'op','generation':1},
                {'configuration':{'heartbeat_interval_seconds':.2,'missed_heartbeats_limit':3}})
            self.assertTrue(json.loads(out)['stopped'])
            self.assertGreater(calls.count('agent.progress'),0)
