"""Bound redundant terminal output copies after durable upload and acknowledgement.

Legacy jobs are never selected. Only generated stdout/stderr paths registered by
this version are eligible; user-created workspace files are not diagnostics.
"""
import hashlib
import json
import os
import time


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS terminal_retention_eligible(id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS terminal_uploads('
               'id TEXT PRIMARY KEY,result TEXT,digest TEXT,stdout TEXT,stderr TEXT,'
               'created REAL,ack INTEGER NOT NULL DEFAULT 0)')


def lookup(db, operation):
    initialize(db)
    row = db.execute('SELECT result FROM terminal_uploads WHERE id=?', (operation,)).fetchone()
    if row is None:
        return False, None
    if row[0] is None:
        raise RuntimeError('Terminal result expired; this operation must not execute again')
    return True, json.loads(row[0])


def cache(db, operation, result, paths):
    initialize(db)
    value = encoded(result)
    digest = hashlib.sha256(value.encode()).hexdigest()
    with db:
        db.execute('INSERT OR IGNORE INTO terminal_uploads VALUES(?,?,?,?,?,?,0)',
                   (operation, value, digest, paths['stdout'], paths['stderr'], time.time()))
        row = db.execute('SELECT digest FROM terminal_uploads WHERE id=?', (operation,)).fetchone()
        if row[0] != digest:
            raise RuntimeError('Terminal result identity conflict')


def output_path(home, operation, name, path):
    expected = os.path.join(os.path.abspath(home), 'jobs', hashlib.sha256(operation.encode()).hexdigest(), name + '.log')
    if os.path.abspath(path) != expected or os.path.realpath(path) != expected:
        raise RuntimeError('Unsafe terminal diagnostic path')
    return expected


def trim(db, home, output_count=100, output_bytes=64 * 1024 * 1024,
         result_count=1000, result_bytes=4 * 1024 * 1024):
    initialize(db)
    limits = (output_count, output_bytes, result_count, result_bytes)
    if any(type(v) is not int or v < 0 for v in limits):
        raise ValueError('Invalid terminal retention limit')
    rows = db.execute('SELECT id,stdout,stderr FROM terminal_uploads '
                      "WHERE stdout!='' OR stderr!='' ORDER BY created DESC,id DESC").fetchall()
    count = 0
    size = 0
    for op, out, err in rows:
        paths = [output_path(home, op, name, p) for name, p in [('stdout', out), ('stderr', err)]] if out and err else []
        total = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        if count < output_count and size + total <= output_bytes:
            count += 1
            size += total
            continue
        # The durable upload result permits delivery/recovery without these files.
        # Remove only these known diagnostics; never recurse through a workspace.
        for p in paths:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        with db:
            db.execute("UPDATE terminal_uploads SET stdout='',stderr='' WHERE id=?", (op,))
    count = 0
    size = 0
    for op, value in db.execute('SELECT id,result FROM terminal_uploads WHERE ack=1 AND result IS NOT NULL ORDER BY created DESC,id DESC').fetchall():
        n = len(value.encode())
        if count < result_count and size + n <= result_bytes:
            count += 1
            size += n
            continue
        with db:
            db.execute('UPDATE terminal_uploads SET result=NULL WHERE id=? AND ack=1', (op,))


def acknowledge(db, operation, digest):
    initialize(db)
    with db:
        row = db.execute('SELECT digest FROM terminal_uploads WHERE id=?', (operation,)).fetchone()
        if row is None or row[0] != digest:
            raise RuntimeError('Terminal acknowledgement mismatch')
        body = db.execute('SELECT body FROM jobs WHERE id=?', (operation,)).fetchone()
        if body:
            old = json.loads(body[0])
            if not old.get('retired'):
                identity = hashlib.sha256(encoded([old['argv'], old['workspace']]).encode()).hexdigest()
                db.execute('UPDATE jobs SET body=?,result=? WHERE id=?',
                           (encoded({'retired': True, 'request_digest': identity}), encoded({'quiescent': True, 'result_uploaded': True}), operation))
        db.execute('UPDATE terminal_uploads SET ack=1 WHERE id=?', (operation,))
