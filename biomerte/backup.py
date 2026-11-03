"""Consistent SQLite copies, including on older SQLite versions without backup APIs."""
import contextlib
import os
import shutil
import sqlite3
import tempfile
from .store import Fault


def backup(app, target):
    target = os.path.abspath(target)
    if os.path.exists(target):
        raise Fault('Backup destination exists')
    tmp = tempfile.mkdtemp(prefix='.biome-backup-', dir=os.path.dirname(target))
    try:
        for realm, store in app.stores.items():
            with contextlib.closing(store.connect()) as src:
                src.execute('BEGIN')
                with contextlib.closing(sqlite3.connect(os.path.join(tmp, os.path.basename(store.path)))) as dst:
                    dst.executescript('\n'.join(src.iterdump()))
                    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise Fault('Backup integrity failure')
        for f in ('node.json', 'node.key'):
            shutil.copy2(os.path.join(app.directory, f), os.path.join(tmp, f))
        os.rename(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {'directory': target, 'contains_private_identity': True}


def restore(source, target):
    if os.path.exists(target):
        raise Fault('Restore needs a new node directory')
    for f in ('node.key', 'node.json'):
        if not os.path.isfile(os.path.join(source, f)):
            raise Fault('Incomplete backup')
    dbs = [f for f in os.listdir(source) if f.endswith('.sqlite')]
    if not dbs:
        raise Fault('Backup has no databases')
    for f in dbs:
        with contextlib.closing(sqlite3.connect(os.path.join(source, f))) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise Fault('Invalid backup database')
    tmp = tempfile.mkdtemp(prefix='.biome-restore-', dir=os.path.dirname(os.path.abspath(target)))
    try:
        for f in dbs + ['node.key', 'node.json']:
            shutil.copy2(os.path.join(source, f), os.path.join(tmp, f))
        import json
        import uuid
        path = os.path.join(tmp, 'node.json')
        with open(path) as f:
            conf = json.load(f)
        conf['incarnation'] = uuid.uuid4().hex
        with open(path, 'w') as f:
            json.dump(conf, f, indent=2)
        with open(os.path.join(tmp, 'RECOVERY_HOLD'), 'w') as f:
            f.write(
                'Retire the old controller, reconcile device actions and reinstall authoritative fences before resuming.\n')
        os.rename(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {'restored': target, 'state': 'recovery_hold'}
