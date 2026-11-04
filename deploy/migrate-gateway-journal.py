#!/usr/bin/env python3
"""Offline migration of an existing controller into the gateway journal.

Stop its runtime and event link first. Existing provider worker ledgers stay on
their hosts. The original database and identity are retained for recovery.
"""
import argparse
import json
import os
import sqlite3
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from biomerte.app import read_json
from biomerte.gateway_journal import GatewayJournal
from biomerte.identity import write_private
from biomerte.store import digest, encode
from ha_link import snapshot


def migrate(home, origin, key_file, controller_id, capacity):
    path = os.path.join(home, 'node.json')
    conf = read_json(path)
    settings = {'origin': origin, 'key_file': key_file,
                'controller_id': controller_id, 'capacity': capacity}
    remote = GatewayJournal(settings, conf['site'])
    db = sqlite3.connect(os.path.join(home, 'site.sqlite'))
    changes = []
    chunks = []
    size = 0
    for kind, key, raw in db.execute('SELECT kind,id,value FROM records ORDER BY kind,id'):
        change = [kind, key, json.loads(raw)]
        count = len(encode(change))
        if size + count > 600000 and changes:
            chunks.append(changes)
            changes, size = [], 0
        changes.append(change)
        size += count
    db.close()
    if changes:
        chunks.append(changes)
    chunks.append([['_meta', 'gateway_migration', {'site': conf['site'], 'source': 'site.sqlite'}]])
    head, unused = remote.read('head')
    if head and head['seq'] > len(chunks):
        raise ValueError('Shared journal already active; migration cannot overwrite it')
    previous = ''
    for number, changes in enumerate(chunks, 1):
        entry = {'seq': number, 'prev': previous, 'changes': changes}
        if head and number <= head['seq']:
            existing, unused = remote.read('log/%020d' % number)
            if digest(existing) != digest(entry):
                raise ValueError('Partial migration has a different source snapshot')
        else:
            remote.append(str(number - 1), entry)
        previous = digest(entry)
    event_db = sqlite3.connect(os.path.join(os.path.dirname(key_file), 'events.sqlite'))
    body = snapshot(event_db)
    event_db.close()
    remote.request('events/seed', {'body': body})
    conf['gateway_journal'] = settings
    write_private(path, (json.dumps(conf, indent=2) + '\n').encode())
    print(json.dumps({'migrated_entries': len(chunks), 'head_hash': previous,
                      'event_tables': len(body), 'site': conf['site']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--controller-id', required=True)
    parser.add_argument('--capacity', type=int, default=1)
    args = parser.parse_args()
    migrate(args.home, args.origin, args.key, args.controller_id, args.capacity)
