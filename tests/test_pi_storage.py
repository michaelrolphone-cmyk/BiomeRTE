"""File-provider behavior and volume-full rollback on SQLite versions supported by Pi."""
import base64
import os
import runpy
import sqlite3
import tempfile
import unittest

storage = runpy.run_path(os.path.join(os.path.dirname(__file__), '..', 'providers', 'pi-storage', 'storage.py'))
BLOCK = storage['BLOCK']


def encode(value):
    return base64.b64encode(value).decode('ascii')


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'files.sqlite')
        self.db = sqlite3.connect(self.path)
        self.db.executescript(storage['SCHEMA'])

    def tearDown(self):
        self.db.close()

    def call(self, op, **args):
        return storage['dispatch'](self.db, op, args)

    def ok(self, op, **args):
        value = self.call(op, **args)
        self.assertTrue(value['ok'], value)
        return value['result']

    def test_all_operations_and_restart(self):
        self.ok('mkdir', path='dir')
        h = self.ok('open', path='dir/data', mode='x+')['handle']
        data = bytes(bytearray(range(256))) * (BLOCK // 256)
        self.ok('write', handle=h, data=encode(data))
        self.ok('write', handle=h, data=encode(b'END'))
        self.ok('write', handle=h, offset=BLOCK-1, data=encode(b'!x'))
        self.ok('rename', path='dir', to='/moved')
        self.db.close()
        self.db = sqlite3.connect(self.path)
        got = self.ok('read', handle=h, offset=BLOCK-2, length=5)
        self.assertEqual(base64.b64decode(got['data']), data[-2:-1] + b'!xND')
        self.assertTrue(got['eof'])
        self.assertEqual(self.ok('read', handle=h)['bytes'], 0)
        self.assertEqual(self.ok('size', path='/moved/data')['size'], BLOCK+3)
        self.assertEqual(self.ok('list', path='moved')['entries'][0]['name'], 'data')
        self.assertFalse(self.ok('exists', path='dir')['exists'])
        self.assertEqual(self.call('delete', path='moved/data')['error'], 'FILE_OPEN')
        self.ok('close', handle=h)
        self.ok('delete', path='moved/data')
        self.ok('rmdir', path='moved')
        self.assertFalse(self.ok('exists', path='moved')['exists'])

    def test_modes_listing_and_path_errors(self):
        self.ok('mkdir', path='/a')
        self.ok('mkdir', path='/a/z')
        self.ok('mkdir', path='/a/y')
        self.assertEqual(self.ok('list', path='/a', limit=1)['next'], 'y')
        self.assertEqual(self.ok('list', path='/a', limit=1, after='y')['entries'][0]['name'], 'z')
        self.assertEqual(self.call('rename', path='/a', to='/a/z/c')['error'], 'DIRECTORY_CYCLE')
        self.assertEqual(self.call('rename', path='/a/z', to='/a/y')['error'], 'ALREADY_EXISTS')
        self.assertEqual(self.call('rmdir', path='/a')['error'], 'NOT_EMPTY')
        for path in ('../../../etc/passwd', '/a/../b', '/./a', 'a\0b', 'x'*256):
            self.assertEqual(self.call('exists', path=path)['error'], 'INVALID_PATH')
        self.assertEqual(self.call('rmdir', path='/')['error'], 'ROOT_PROTECTED')
        a = self.ok('open', path='/f', mode='a')['handle']
        h = self.ok('open', path='/f', mode='w+')['handle']
        self.assertEqual(self.call('open', path='/f', mode='x')['error'], 'ALREADY_EXISTS')
        self.assertEqual(self.call('read', handle=a)['error'], 'NOT_READABLE')
        self.assertEqual(self.call('write', handle=a, offset=0, data='')['error'], 'APPEND_OFFSET')
        self.assertEqual(self.call('write', handle=h, data='%%')['error'], 'INVALID_BASE64')
        self.assertEqual(self.call('read', handle=h, length=BLOCK+1)['error'], 'INVALID_LENGTH')
        for token, byte in ((a, b'a'), (h, b'b'), (a, b'c')):
            self.ok('write', handle=token, data=encode(byte))
        self.assertEqual(self.ok('read', handle=h, offset=0)['data'], encode(b'bc'))
        self.ok('open', path='/f', mode='w')
        self.assertTrue(self.ok('read', handle=h, offset=0)['eof'])
        self.ok('close', handle=h)
        self.assertEqual(self.call('read', handle=h)['error'], 'INVALID_HANDLE')

    def test_full_volume_rolls_back_and_deleted_space_can_be_reused(self):
        self.db.execute('PRAGMA max_page_count=120')
        h = self.ok('open', path='/big', mode='w+')['handle']
        chunk = encode(b'z' * BLOCK)
        used = 0
        for i in range(32):
            v = self.call('write', handle=h, data=chunk)
            if not v['ok']:
                self.assertEqual(v['error'], 'NO_SPACE')
                break
            used += BLOCK
        else:
            self.fail('quota was not reached')
        self.assertGreater(used, 0)
        self.assertEqual(self.ok('size', path='/big')['size'], used)
        self.assertEqual(self.ok('read', handle=h, length=1)['offset'], used)
        self.ok('close', handle=h)
        self.ok('delete', path='/big')
        h = self.ok('open', path='/new', mode='x+')['handle']
        self.assertEqual(self.ok('write', handle=h, data=chunk)['size'], BLOCK)
