import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from biomerte.command_watch import capture, communicate
from biomerte.store import Fault


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

    def test_unavailable_control_read_does_not_skip_heartbeat(self):
        class Remote:
            node = 'provider'

            def __init__(self):
                self.heartbeats = []

            def call(self, name, args):
                if name == 'cap.command.state':
                    raise Fault('Control read unavailable', 503)
                if name == 'cap.heartbeat':
                    self.heartbeats.append(args)

        with tempfile.TemporaryDirectory() as directory:
            remote = Remote()
            worker = types.SimpleNamespace(remote=remote)
            path = os.path.join(directory, 'progress')
            with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.25); print(42)'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                out, _ = communicate(worker, child, {'progress_path': path}, {'id': 'op', 'generation': 8},
                                     {'configuration': {'heartbeat_interval_seconds': .1}})
            self.assertEqual(out.strip(), b'42')
            self.assertIn({'provider': 'provider', 'operation': 'op', 'generation': 8}, remote.heartbeats)
            self.assertFalse(os.path.exists(path + '.stop'))

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
