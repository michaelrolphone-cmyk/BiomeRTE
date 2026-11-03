import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parents[1]/'providers/mac-storage'
sys.path.insert(0,str(HERE))
import backup

class MacStorage(unittest.TestCase):
    def setUp(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        self.root=Path(d.name)
        self.vault=self.root/'vault.tar'
        self.budget=4*1024**2
        def reserve(p,n):
            with p.open('xb') as f:f.write(b'\0'*n)
        reserve(self.vault,self.budget)
        with self.vault.open('r+b') as f,tarfile.open(fileobj=f,mode='w'):pass
        self.addCleanup(mock.patch.stopall)
        mock.patch.multiple(backup,BASE=self.root,VAULT=self.vault,SACRED=self.budget,allocate=reserve).start()
    def generation(self,value):
        with tempfile.TemporaryDirectory(dir=self.root) as tmpd:
            tmp=Path(tmpd);raw=tmp/'db'
            db=sqlite3.connect(raw)
            db.executescript("create table volume(uuid);insert into volume values('abc');create table demo(value);")
            db.execute('insert into demo values(?)',(value,));db.commit();db.close()
            meta=dict(volume='abc',sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),size=raw.stat().st_size,stamp=[value])
            with tarfile.open(self.vault) as old:
                return backup.publish(raw,meta,old,backup.entries(old),tmp)
    def test_history_restore_and_failed_publication(self):
        a=self.generation('first');b=self.generation('second')
        self.assertEqual(self.vault.stat().st_size,self.budget)
        with mock.patch.object(backup.os,'replace',side_effect=OSError('simulated full disk')):
            with self.assertRaises(OSError):self.generation('third')
        for row,value in [(a,'first'),(b,'second')]:
            dest=self.root/(value+'.sqlite');backup.restore(dest,row['id'],self.vault)
            with sqlite3.connect(dest) as db:self.assertEqual(db.execute('select value from demo').fetchone(),(value,))
            self.assertEqual(hashlib.sha256(dest.read_bytes()).hexdigest(),row['sha256'])
            with self.assertRaises(FileExistsError):backup.restore(dest,row['id'],self.vault)
    def test_corruption_refuses_restore(self):
        g=self.generation('trusted')
        with tarfile.open(self.vault) as ar:offset=ar.getmember(g['file']).offset_data
        with self.vault.open('r+b') as f:f.seek(offset);f.write(b'BAD!')
        target=self.root/'bad.sqlite'
        with self.assertRaises(OSError):backup.restore(target,g['id'],self.vault)
        self.assertFalse(target.exists())
    def test_namespace_bound_paths_and_handles(self):
        with mock.patch.dict(sys.modules,{'mac_provider':types.ModuleType('mac_provider')}):
            p=importlib.import_module('provider')
        with mock.patch.multiple(p,BASE=self.root,MOUNT=self.root,mounted=lambda:True):
            for ns in ('one','two'):self.assertTrue(p.operation('create',{'namespace':ns})['ok'])
            self.assertEqual(p.operation('list',{'path':'/'})['error'],'INVALID_NAMESPACE')
            self.assertEqual(p.operation('open',{'namespace':'one','path':'../two/x','mode':'w'})['error'],'INVALID_PATH')
            h=p.operation('open',{'namespace':'one','path':'/x','mode':'x+'})['result']['handle']
            self.assertEqual(p.operation('write',{'namespace':'two','handle':h,'data':'YQ=='})['error'],'INVALID_HANDLE')
            self.assertFalse(p.operation('exists',{'namespace':'two','path':'/x'})['result']['exists'])
            self.assertEqual(p.operation('remove',{'namespace':'one'})['error'],'NAMESPACE_NOT_EMPTY')
        with mock.patch.object(p,'mounted',return_value=False):
            self.assertEqual(p.operation('create',{'namespace':'oops'})['error'],'WORKSPACE_OFFLINE')
