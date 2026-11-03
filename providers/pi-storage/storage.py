"""Transactional file operations for the separately mounted Pi storage volume."""
import base64
import binascii
from contextlib import contextmanager
import json
import os
import sqlite3
import sys
import uuid

BLOCK = 64 * 1024
# Each operation is separately discoverable, with a fixed adapter entry point.
OPERATIONS = {
    'open': ({'path': 'str', 'mode': 'str'}, 'Open a file with mode r, w, a, x or a + variant.'),
    'read': ({'handle': 'str', 'length': 'int?', 'offset': 'int?'}, 'Read up to 65536 bytes as base64.'),
    'write': ({'handle': 'str', 'data': 'str', 'offset': 'int?'}, 'Write up to 65536 base64-decoded bytes.'),
    'close': ({'handle': 'str'}, 'Close a persistent file handle.'),
    'delete': ({'path': 'str'}, 'Delete a closed file.'),
    'rename': ({'path': 'str', 'to': 'str'}, 'Move a file or directory without replacing its destination.'),
    'exists': ({'path': 'str'}, 'Check whether a file or directory exists.'),
    'size': ({'path': 'str'}, 'Get the size of a file in bytes.'),
    'list': ({'path': 'str', 'after': 'str?', 'limit': 'int?'}, 'List immediate children in name order.'),
    'mkdir': ({'path': 'str'}, 'Create a directory in an existing parent.'),
    'rmdir': ({'path': 'str'}, 'Remove an empty directory.')
}
OUTPUTS = {'ok': 'bool', 'result': 'obj?', 'error': 'str?'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS volume(uuid TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(
 id INTEGER PRIMARY KEY AUTOINCREMENT, gen INTEGER NOT NULL DEFAULT 0, parent INTEGER REFERENCES nodes, name TEXT NOT NULL,
 kind TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, UNIQUE(parent,name));
INSERT OR IGNORE INTO nodes(id,parent,name,kind) VALUES(1,NULL,'','directory');
CREATE TABLE IF NOT EXISTS blocks(
 node INTEGER, gen INTEGER, idx INTEGER, data BLOB NOT NULL,
 PRIMARY KEY(node,gen,idx)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS garbage(node INTEGER, gen INTEGER, PRIMARY KEY(node,gen));
CREATE TABLE IF NOT EXISTS handles(
 token TEXT PRIMARY KEY, node INTEGER NOT NULL REFERENCES nodes, mode TEXT NOT NULL, pos INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS handles_by_node ON handles(node);
"""


class FileError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise FileError(code)


def components(path):
    try:
        path.encode('utf-8')
    except UnicodeError:
        raise FileError('INVALID_PATH')
    require(path and '\x00' not in path and len(path.encode('utf-8')) <= 4096, 'INVALID_PATH')
    parts = [p for p in path.split('/') if p]
    require(all(p not in ('.', '..') and len(p.encode('utf-8')) <= 255 for p in parts), 'INVALID_PATH')
    return parts


def lookup(db, parts):
    row = db.execute('SELECT id,kind,size FROM nodes WHERE id=1').fetchone()
    for part in parts:
        require(row[1] == 'directory', 'NOT_DIRECTORY')
        row = db.execute('SELECT id,kind,size FROM nodes WHERE parent=? AND name=?',
                         (row[0], part)).fetchone()
        require(row, 'NOT_FOUND')
    return row


def named_parent(db, parts):
    require(parts, 'ROOT_PROTECTED')
    parent, kind, _ = lookup(db, parts[:-1])
    require(kind == 'directory', 'NOT_DIRECTORY')
    return parent, parts[-1]


def open_file(db, parts, mode):
    require(mode in ('r', 'w', 'a', 'x', 'r+', 'w+', 'a+', 'x+'), 'INVALID_MODE')
    parent, name = named_parent(db, parts)
    row = db.execute('SELECT id,kind,size FROM nodes WHERE parent=? AND name=?', (parent, name)).fetchone()
    if row is None:
        require(mode[0] != 'r', 'NOT_FOUND')
        node = db.execute("INSERT INTO nodes(parent,name,kind) VALUES(?,?,'file')", (parent, name)).lastrowid
        size = 0
    else:
        node, kind, size = row
        require(kind == 'file', 'IS_DIRECTORY')
        require(mode[0] != 'x', 'ALREADY_EXISTS')
        if mode[0] == 'w':
            retire_blocks(db, node)
            db.execute('UPDATE nodes SET size=0,gen=gen+1 WHERE id=?', (node,))
            size = 0
    token = uuid.uuid4().hex
    pos = size if mode[0] == 'a' else 0
    db.execute('INSERT INTO handles VALUES(?,?,?,?)', (token, node, mode, pos))
    return {'handle': token, 'offset': pos, 'size': size}


def transfer(db, op, a):
    token = a['handle']
    row = db.execute('SELECT h.node,h.mode,h.pos,n.size,n.gen FROM handles h JOIN nodes n ON n.id=h.node '
                     'WHERE h.token=?', (token,)).fetchone()
    require(row, 'INVALID_HANDLE')
    node, mode, cursor, size, gen = row
    pos = a.get('offset', cursor)
    require(0 <= pos <= 0x7fffffffffffffff, 'INVALID_OFFSET')
    if op == 'read':
        require(mode[0] == 'r' or '+' in mode, 'NOT_READABLE')
        length = a.get('length', BLOCK)
        require(0 <= length <= BLOCK, 'INVALID_LENGTH')
        end = pos + min(length, max(0, size - pos))
        data = bytearray()
        while pos < end:
            index, start = divmod(pos, BLOCK)
            block = db.execute('SELECT data FROM blocks WHERE node=? AND gen=? AND idx=?', (node, gen, index)).fetchone()[0]
            count = min(end - pos, BLOCK - start)
            data.extend(block[start:start + count])
            pos += count
        result = {'data': base64.b64encode(data).decode('ascii'), 'bytes': len(data), 'eof': pos >= size}
    else:
        require(mode != 'r', 'NOT_WRITABLE')
        if mode[0] == 'a':
            require('offset' not in a, 'APPEND_OFFSET')
            pos = size
        require(pos <= size, 'OFFSET_PAST_END')
        require(len(a['data']) <= 4 * ((BLOCK + 2) // 3), 'CHUNK_TOO_LARGE')
        try:
            data = base64.b64decode(a['data'].encode('ascii'), validate=True)
        except (ValueError, binascii.Error):
            raise FileError('INVALID_BASE64')
        require(len(data) <= BLOCK, 'CHUNK_TOO_LARGE')
        consumed = 0
        while consumed < len(data):
            index, start = divmod(pos, BLOCK)
            count = min(BLOCK - start, len(data) - consumed)
            old = db.execute('SELECT data FROM blocks WHERE node=? AND gen=? AND idx=?', (node, gen, index)).fetchone()
            block = bytearray(old[0] if old else b'')
            block[start:start + count] = data[consumed:consumed + count]
            db.execute('INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)', (node, gen, index, bytes(block)))
            pos += count
            consumed += count
        db.execute('UPDATE nodes SET size=? WHERE id=?', (max(size, pos), node))
        result = {'bytes': consumed, 'size': max(size, pos)}
    db.execute('UPDATE handles SET pos=? WHERE token=?', (pos, token))
    result['offset'] = pos
    return result


def apply(db, op, a):
    if op in ('read', 'write'):
        return transfer(db, op, a)
    if op == 'close':
        require(db.execute('DELETE FROM handles WHERE token=?', (a['handle'],)).rowcount, 'INVALID_HANDLE')
        return {}
    parts = components(a['path'])
    if op == 'open':
        return open_file(db, parts, a['mode'])
    if op == 'mkdir':
        parent, name = named_parent(db, parts)
        db.execute("INSERT INTO nodes(parent,name,kind) VALUES(?,?,'directory')", (parent, name))
        return {}
    try:
        node, kind, size = lookup(db, parts)
    except FileError as exc:
        if op == 'exists' and str(exc) in ('NOT_FOUND', 'NOT_DIRECTORY'):
            return {'exists': False}
        raise
    if op == 'exists':
        return {'exists': True, 'type': kind}
    if op == 'size':
        require(kind == 'file', 'IS_DIRECTORY')
        return {'size': size}
    if op == 'list':
        require(kind == 'directory', 'NOT_DIRECTORY')
        limit = a.get('limit', 100)
        require(1 <= limit <= 200, 'INVALID_LIMIT')
        after = a.get('after', '')
        if after:
            require(len(components(after)) == 1 and '/' not in after, 'INVALID_CURSOR')
        rows = db.execute('SELECT name,kind,size FROM nodes WHERE parent=? AND name>? ORDER BY name LIMIT ?',
                          (node, after, limit + 1)).fetchall()
        return {'entries': [dict(name=n, type=k, size=s) for n, k, s in rows[:limit]],
                'next': rows[limit-1][0] if len(rows) > limit else None}
    require(node != 1, 'ROOT_PROTECTED')
    if op == 'rename':
        dest = components(a['to'])
        if parts == dest:
            return {}
        require(kind != 'directory' or dest[:len(parts)] != parts, 'DIRECTORY_CYCLE')
        parent, name = named_parent(db, dest)
        db.execute('UPDATE nodes SET parent=?,name=? WHERE id=?', (parent, name, node))
    elif op == 'delete':
        require(kind == 'file', 'IS_DIRECTORY')
        require(not db.execute('SELECT 1 FROM handles WHERE node=? LIMIT 1', (node,)).fetchone(), 'FILE_OPEN')
        retire_blocks(db, node)
        db.execute('DELETE FROM nodes WHERE id=?', (node,))
    elif op == 'rmdir':
        require(kind == 'directory', 'NOT_DIRECTORY')
        require(not db.execute('SELECT 1 FROM nodes WHERE parent=? LIMIT 1', (node,)).fetchone(), 'NOT_EMPTY')
        db.execute('DELETE FROM nodes WHERE id=?', (node,))
    else:
        raise ValueError('Unknown operation')
    return {}


def retire_blocks(db, node):
    db.execute('INSERT OR IGNORE INTO garbage SELECT id,gen FROM nodes WHERE id=? AND size>0', (node,))


def reclaim(db):
    # Keep rollback journals small even when truncating a nearly full volume.
    row = db.execute('SELECT node,gen FROM garbage LIMIT 1').fetchone()
    if row:
        blocks = db.execute('SELECT idx FROM blocks WHERE node=? AND gen=? LIMIT 8', row).fetchall()
        db.executemany('DELETE FROM blocks WHERE node=? AND gen=? AND idx=?',
                       [(row[0], row[1], idx) for idx, in blocks])
        if len(blocks) < 8:
            db.execute('DELETE FROM garbage WHERE node=? AND gen=?', row)


@contextmanager
def transaction(db):
    # Explicit SQL also works on the Pi's pre-3.6 sqlite3 transaction driver.
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise


def dispatch(db, op, args):
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with transaction(db):
            reclaim(db)
        with transaction(db):
            return {'ok': True, 'result': apply(db, op, args)}
    except FileError as exc:
        return {'ok': False, 'error': str(exc)}
    except sqlite3.IntegrityError as exc:
        if 'UNIQUE' in str(exc):
            return {'ok': False, 'error': 'ALREADY_EXISTS'}
        raise
    except sqlite3.OperationalError as exc:
        if 'database or disk is full' in str(exc):
            return {'ok': False, 'error': 'NO_SPACE'}
        raise


def main(config, op):
    with open(config) as f:
        cfg = json.load(f)
    root = cfg['mount']
    if not os.path.ismount(root):
        return {'ok': False, 'error': 'VOLUME_OFFLINE'}
    # mode=rw prevents a missing or moved volume from creating a new database.
    from urllib.parse import quote
    uri = 'file:' + quote(os.path.join(root, 'biomerte-files', 'files.sqlite')) + '?mode=rw'
    db = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        fs = os.statvfs(root)
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        # Reserve journal headroom on the dedicated mount for cleanup at capacity.
        max_pages = max(1, (fs.f_blocks * fs.f_frsize - 16 * 1024 ** 2) // page_size)
        db.execute('PRAGMA max_page_count=%d' % max_pages)
        if db.execute('SELECT uuid FROM volume').fetchone() != (cfg['volume'],):
            return {'ok': False, 'error': 'WRONG_VOLUME'}
        return dispatch(db, op, json.load(sys.stdin)['inputs'])
    finally:
        db.close()


if __name__ == '__main__':
    json.dump(main(sys.argv[1], sys.argv[2]), sys.stdout)
