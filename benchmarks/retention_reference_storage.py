"""Synthetic storage qualification, never opens an existing controller database.

60k nested canonical records totaling ~200 MiB by default. Repeated field names,
64-character IDs, unrelated short strings and opaque output exercise general
reference semantics. This is a reproducible stress fixture, not a live-data claim.
Run from repository root: PYTHONPATH=. python benchmarks/retention_reference_storage.py /tmp/new.sqlite
"""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from biomerte import retention_index


def run(path, rows, mib):
    if os.path.exists(path):
        raise ValueError('Benchmark requires a new file')
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
    target = int(mib * 1048576 / rows)
    raw = 0
    for n in range(rows):
        key = hashlib.sha256(('record-%d' % n).encode()).hexdigest()
        value = {'id': key, 'state': 'succeeded', 'provider': 'terminal.mac.worker',
                 'contract': 'terminal.exec:1', 'resource': 'terminal.mac.slot',
                 'lease': hashlib.sha256(('lease-%d' % n).encode()).hexdigest(),
                 'inputs': {'argv': ['python3', '-c', 'print(1)'], 'scope': 'site'},
                 'result': {'state': 'completed', 'evidence': {'quiescent': True},
                            'nested': [{'name': 'step-%d' % step,
                                        'ref': hashlib.sha256(('record-%d' % ((n + step + 1) % rows)).encode()).hexdigest(),
                                        'status': 'complete', 'note': 'ordinary diagnostic text'}
                                       for step in range(8)]},
                 'opaque_output': ''}
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
        value['opaque_output'] = 'x' * max(0, target - len(encoded))
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
        raw += len(encoded)
        db.execute('INSERT INTO records VALUES(?,?,?)', ('actions' if n % 3 else 'durable_unknown', key, encoded))
    db.commit()
    before_pages = db.execute('PRAGMA page_count').fetchone()[0]
    retention_index.initialize(db)
    db.commit()
    durations, started = [], time.monotonic()
    while True:
        before = time.monotonic()
        done = retention_index.advance(db)
        db.commit()
        durations.append(time.monotonic() - before)
        if done:
            break
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    size = db.execute('PRAGMA page_size').fetchone()[0]
    pages = db.execute('PRAGMA page_count').fetchone()[0]
    details = [dict(name=name, pages=count, bytes=total) for name, count, total in db.execute(
        'SELECT name,count(*),sum(pgsize) FROM dbstat GROUP BY name ORDER BY name')]
    print(json.dumps({'rows': rows, 'raw_json_bytes': raw, 'canonical_pages': before_pages,
                      'total_pages': pages, 'page_size': size,
                      'projection_allocated_bytes': (pages - before_pages) * size,
                      'reference_entries': db.execute('SELECT count(*) FROM retention_refs').fetchone()[0],
                      'build_seconds': time.monotonic() - started, 'ticks': len(durations),
                      'tick_seconds_max': max(durations),
                      'tick_seconds_p95': sorted(durations)[int(.95 * (len(durations)-1))],
                      'objects': details}, sort_keys=True, indent=2))
    db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('database')
    parser.add_argument('--rows', type=int, default=60000)
    parser.add_argument('--mib', type=float, default=200)
    args = parser.parse_args()
    run(args.database, args.rows, args.mib)
