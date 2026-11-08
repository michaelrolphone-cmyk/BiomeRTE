"""Concurrent controller inbox and fenced, replicated event delivery."""
import concurrent.futures
import json
import os
import socket
import sqlite3
import threading
import time

TABLES = ('subscriptions', 'receiver_receipts', 'failed_events')


def snapshot(db):
    result = []
    for table in TABLES:
        columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
        if columns:
            result.append({'table': table, 'columns': columns,
                           'rows': [list(row) for row in db.execute('SELECT * FROM ' + table)]})
    return result


def restore(db, body):
    db.execute('CREATE TABLE IF NOT EXISTS failed_events (sub TEXT,event TEXT,status INTEGER,PRIMARY KEY(sub,event))')
    with db:
        for table in TABLES:
            db.execute('DELETE FROM ' + table)
        for item in body or []:
            table = item['table']
            if table not in TABLES:
                raise ValueError('Unknown event table')
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
            if any(col not in columns for col in item['columns']):
                raise ValueError('Unknown event column')
            query = 'INSERT INTO ' + table + '(' + ','.join(item['columns']) + ') VALUES (' + ','.join('?' for col in item['columns']) + ')'
            db.executemany(query, item['rows'])


def run(args, origin, token, site_access, fetch, dispatch, event_service, event_change):
    with open(os.path.join(args.home, 'node.json')) as handle:
        conf = json.load(handle)
    settings = conf['gateway_journal']
    local = 'http://127.0.0.1:' + str(args.port)
    status = {'primary': None, 'ready': False}
    event_lock = threading.Lock()
    from persistent_link import ControllerLink
    from result_delivery import complete
    persistent = ControllerLink(origin, token, site_access)

    def call(path, body, delivery_socket=None, timeout=45):
        def send(target, payload):
            return fetch(origin + '/bridge/' + target, payload, token, timeout=timeout, site_access=site_access)
        if path == 'complete':
            return complete(body, send, persistent, delivery_socket)
        return send(path, body)

    def heartbeat():
        while True:
            try:
                health = fetch(local + '/health')
                db = sqlite3.connect(os.path.join(args.home, 'gateway-' + conf['site'] + '.sqlite'))
                try:
                    position = db.execute('SELECT seq FROM position WHERE id=1').fetchone()[0]
                finally:
                    db.close()
                observed = call('ha/heartbeat', {'position': position,
                    'ready': health.get('realms', {}).get('site') == 'ready',
                    'load': os.getloadavg()[0] / (os.cpu_count() or 1), 'host': socket.gethostname()})
                status.update(observed)
                status['ready'] = health.get('realms', {}).get('site') == 'ready'
            except Exception:
                status['ready'] = False
            time.sleep(settings.get('heartbeat_interval', 2))

    def event_action(action):
        with event_lock:
            lease = call('ha/events/claim', {})
            if not lease.get('acquired'):
                raise RuntimeError('Event ownership moving to preferred controller')
            fence = lease['fence']
            stopped = threading.Event()
            valid = [True]

            def renew():
                while not stopped.wait(5):
                    try:
                        call('ha/events/renew', {'fence': fence})
                    except Exception:
                        valid[0] = False
                        return

            thread = threading.Thread(target=renew)
            thread.daemon = True
            thread.start()
            events = event_service(':memory:', local, args.home, origin, token, site_access)
            try:
                restore(events.db, lease['body'])
                original_post = events.post

                def post(*values):
                    if not valid[0]:
                        raise RuntimeError('Event lease lost')
                    call('ha/events/renew', {'fence': fence})
                    return original_post(*values)

                events.post = post
                result = action(events)
                if not valid[0]:
                    raise RuntimeError('Event lease lost')
                call('ha/events/commit', {'fence': fence, 'body': snapshot(events.db)})
                return result
            finally:
                stopped.set()
                thread.join()
                events.close()

    def deliver():
        while True:
            if status.get('primary') == settings['controller_id'] and status['ready']:
                try:
                    event_action(lambda events: events.deliver() if events.active() else None)
                except Exception as exc:
                    print('HA event delivery retry: ' + type(exc).__name__, flush=True)
            time.sleep(2)

    targets = [heartbeat, deliver]
    if settings.get('checkpoint_mode', 'off') in ('bootstrap', 'maintain'):
        from checkpoint_maintenance import run as maintain_checkpoints
        checkpoint_stopped = threading.Event()
        targets.append(lambda: maintain_checkpoints(settings, status, call, checkpoint_stopped))
    for target in targets:
        thread = threading.Thread(target=target)
        thread.daemon = True
        thread.start()

    def process(job):
        try:
            name = job['request']['name']
            values = job['request'].get('arguments', {})
            if name == 'grok_receiver_change':
                from grok_receivers import mutate
                value = event_action(lambda events: mutate(events, values['action'], values['args'], values['owner'], values['grant']))
                result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
            elif name == 'mcp_event_change':
                result = event_action(lambda events: event_change(events, values))
            else:
                result = dispatch(job, local, conf)
                if name == 'biome_status':
                    value = json.loads(result['content'][0]['text'])
                    value['cluster'] = call('ha/status', {})
                    result['content'][0]['text'] = json.dumps(value)
            call('complete', {'id': job['id'], 'lease': job['lease'], 'result': result},
                 job.get('_delivery_socket'))
        except Exception as exc:
            # Keep uncertain requests reclaimable with their original identity.
            print('HA command retry: ' + type(exc).__name__, flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=settings.get('capacity', 1)) as pool:
        active = set()
        while True:
            active = {future for future in active if not future.done()}
            if len(active) >= settings.get('capacity', 1) or not status['ready']:
                time.sleep(.1)
                continue
            try:
                job = persistent.next()
                if job is None:
                    job = call('next', {})
                if job.get('id'):
                    active.add(pool.submit(process, job))
                else:
                    time.sleep(.05 if persistent.socket is not None else .4)
            except Exception as exc:
                print('HA link retry: ' + type(exc).__name__, flush=True)
                time.sleep(2)

