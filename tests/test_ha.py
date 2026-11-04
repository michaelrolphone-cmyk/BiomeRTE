import copy
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.store import digest
from biomerte.gateway_journal import JournalConflict

class Journal:
    def __init__(self):
        self.entries = []
        self.lock = threading.Lock()
    def read(self, name):
        with self.lock:
            if name == 'head':
                return ({'seq': len(self.entries), 'hash': digest(self.entries[-1])} if self.entries else None), str(len(self.entries))
            return copy.deepcopy(self.entries[int(name[4:])-1]), '0'
    def append(self, expected, entry):
        with self.lock:
            if int(expected) != len(self.entries):
                raise JournalConflict('concurrent write',409)
            self.entries.append(copy.deepcopy(entry))
            return {'seq': len(self.entries), 'hash': digest(entry)}

class HA(unittest.TestCase):
    def test_two_controllers_receipts_concurrency_and_rejoin(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=Journal()
            apps=[]
            for name in ('pi','mac'):
                home=os.path.join(directory,name);initialize(home,'same-site')
                app=Application(home);app.stores['site'].remote=journal;apps.append(app)
            args={'kind':'task','title':'Retained work','id':'ha-work'}
            one=apps[0].call('work.create',args,'same-command')
            self.assertEqual(one,apps[1].call('work.create',args,'same-command'))
            errors=[]
            def write(app,prefix):
                try:
                    for i in range(10):app.call('work.create',dict(args,id=prefix+str(i)),prefix+str(i))
                except Exception as exc:errors.append(exc)
            threads=[threading.Thread(target=write,args=(a,str(i))) for i,a in enumerate(apps)]
            for t in threads:t.start()
            for t in threads:t.join()
            self.assertEqual(errors,[])
            self.assertEqual(apps[0].call('work.list'),apps[1].call('work.list'))
            self.assertEqual(len(apps[0].call('work.list')),21)

    def test_timer_deadline_survives_controller_replacement(self):
        with tempfile.TemporaryDirectory() as home:
            initialize(home);app=Application(home)
            app.call('routine.publish',{'name':'ha-wait','steps':[{'type':'timer','seconds':60}], 'inputs':{},'outputs':{},'result':{}},'define')
            with patch('time.time',return_value=1000):
                run=app.call('process.start',{'name':'ha-wait','inputs':{}},'start')
                app.tick('site')
            app=Application(home)
            with patch('time.time',return_value=1061):
                for _ in range(10):app.tick('site')
            self.assertEqual(app.call('process.get',{'run':run['id']})['state'],'completed')

    def test_head_fetch_does_not_hold_local_writer_lock(self):
        with tempfile.TemporaryDirectory() as home:
            initialize(home);app=Application(home);journal=Journal()
            app.stores['site'].remote=journal
            app.call('work.create',{'kind':'task','title':'Shared','id':'shared'},'create')
            barrier=threading.Barrier(2,timeout=3)
            original=journal.read
            def read(name):
                if name=='head':barrier.wait()
                return original(name)
            journal.read=read
            errors=[]
            def query():
                try:app.call('work.list')
                except Exception as exc:errors.append(exc)
            threads=[threading.Thread(target=query) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
            self.assertEqual(errors,[])
