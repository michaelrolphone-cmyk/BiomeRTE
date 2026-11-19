"""Read-only workflow views for the authenticated operator dashboard."""
import json
import os
import sqlite3
import sys
from glob import glob


def _home(conf):
    value = conf.get('_dashboard_home')
    if not value and '--home' in sys.argv:
        value = sys.argv[sys.argv.index('--home') + 1]
    return value or os.environ.get('BIOME_HOME') or os.path.expanduser('~/.local/share/biomerte')


def read_dashboard_definition(args, conf):
    realm = args.get('realm', 'site')
    if realm not in ('site', 'local'):
        return {'status': 400, 'body': {'error': 'Invalid workspace'}}
    home = _home(conf)
    paths = ([os.path.join(home, 'local.sqlite')] if realm == 'local' else
             [os.path.join(home, 'site.sqlite')] if conf.get('gateway_link') else
             sorted(glob(os.path.join(home, 'gateway-*.sqlite')), key=os.path.getmtime, reverse=True) +
             [os.path.join(home, 'site.sqlite')])
    db = None
    for path in paths:
        try:
            candidate = sqlite3.connect('file:' + path + '?mode=ro', uri=True, timeout=12)
            candidate.execute('SELECT kind FROM records LIMIT 1').fetchone()
            db = candidate
            break
        except sqlite3.DatabaseError:
            if 'candidate' in locals():
                candidate.close()
    if db is None:
        return {'status': 503, 'body': {'error': 'The workflow view is not synchronized on this controller yet.'}}
    try:
        db.execute('BEGIN')
        def record(kind, key):
            row = db.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
            return json.loads(row[0]) if row else None
        if args.get('mode') == 'summaries':
            result = {}
            for row in db.execute("SELECT value FROM records WHERE kind='runs'"):
                run = json.loads(row[0])
                summary = {k: run[k] for k in ('id', 'name', 'rev', 'state', 'steps', 'task',
                           'created_at', 'started_at', 'completed_at', 'finished_at', 'started') if k in run}
                version = record('process_versions', run.get('rev', '')) or {}
                graph = version.get('graph', {}).get('nodes', {})
                current = []
                for token in run.get('tokens', []):
                    node_id = token.get('node') or token.get('at') or token.get('node_id')
                    node = graph.get(node_id, {})
                    item = {k: node[k] for k in ('name', 'label', 'title', 'type', 'operation', 'contract', 'process') if k in node}
                    item.update(id=node_id, state=token.get('state') or token.get('phase', 'active'))
                    current.append(item)
                summary['current_nodes'] = current
                result[run['id']] = summary
            return {'status': 200, 'body': {'result': {'runs': result}}}
        name = args.get('name')
        revision = args.get('revision')
        if not revision:
            definition = record('definitions', name)
            if not definition:
                return {'status': 404, 'body': {'error': 'Workflow not found'}}
            revision = definition['rev']
        version = record('process_versions', revision)
        if not version or version.get('name') != name:
            return {'status': 404, 'body': {'error': 'The requested workflow revision is not available'}}
        return {'status': 200, 'body': {'result': version}}
    finally:
        db.close()
