"""A hardware/service adapter with durable fences and a never-blind-replay ledger."""
import json
import os
import sqlite3
import subprocess
import time
import ssl
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPSHandler
from .store import Fault, encode, uid, mkdir, digest
from .identity import sign


def post(url, path, body, ca=None, token=None, timeout=30):
    parsed = urlparse(url)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise Fault('Remote connections require TLS or an SSH tunnel')
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url.rstrip('/') + path, encode(body).encode(), headers)
    # HTTPSHandler supports the Jessie Python 3.4.2 API as well as current Python.
    handlers = [HTTPSHandler(context=ssl.create_default_context(cafile=ca or None))] if parsed.scheme == 'https' else []
    with build_opener(*handlers).open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())['result']


class Remote:
    def __init__(self, url, site, node, key, ca=None, token=None, realm='site'):
        self.url, self.site, self.node, self.key = url, site, node, key
        self.ca, self.token, self.realm = ca, token, realm

    def call(self, name, args, rid=None):
        payload = {'id': rid or uid(), 'operation': name, 'realm': self.realm,
                   'arguments': args, 'site': self.site}
        if not self.token:
            payload['auth'] = {'actor': self.node, 'signature': sign(self.key, payload)}
        return post(self.url, '/v1/call', payload, self.ca, self.token)


class Worker:
    def __init__(self, path, remote, offers, timeout=30):
        mkdir(os.path.dirname(os.path.abspath(path)))
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS fences(resource TEXT PRIMARY KEY,gen INTEGER,holder TEXT);'
                              'CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,signature TEXT,state TEXT,result TEXT,resource TEXT);')
        self.remote, self.offers, self.timeout = remote, offers, timeout
        self.physical = {x['resource'] for x in offers}
        # Pair this durable enforcement store with one authority namespace.
        self.db.execute('CREATE TABLE IF NOT EXISTS authority(id INTEGER PRIMARY KEY,value TEXT)')
        authority = encode([remote.site, remote.realm, remote.node])
        row = self.db.execute('SELECT value FROM authority WHERE id=1').fetchone()
        if row and row[0] != authority:
            raise Fault('Worker ledger belongs to another authority')
        self.db.execute('INSERT OR IGNORE INTO authority VALUES(1,?)', (authority,))

    def advertise(self):
        for o in self.offers:
            args = {'provider': self.remote.node, 'contract': o['contract'], 'resource': o['resource'],
                    'service': o.get('service', ''), 'state': o.get('state', 'ready')}
            for field in ('capacity', 'usage'):
                if field in o:
                    args[field] = o[field]
            self.remote.call('cap.offer', args)


    def reconcile_guard(self, guard, exclude=None):
        """Adopt only exact, explicitly quiescent controller reconciliations."""
        rows = list(self.db.execute(
            "SELECT id,signature,result FROM actions WHERE state='uncertain' AND resource=?", (guard,)))
        for operation, signature, old_result in rows:
            if operation == exclude:
                continue
            a = self.remote.call('cap.status', {'operation': operation})
            if a.get('provider') != self.remote.node or a.get('guard', a.get('resource')) != guard:
                raise Fault('Reconciliation binding mismatch', 409)
            if signature != digest([a['contract'], a['inputs'], a['resource'], a['generation'], a['lease']]):
                raise Fault('Reconciliation identity mismatch', 409)
            result = a.get('result', {})
            if a.get('state') not in ('succeeded', 'failed') or not (
                    result.get('reconciliation', {}).get('quiescent') is True or
                    result.get('evidence', {}).get('quiescent') is True):
                continue
            with self.db:
                self.db.execute('CREATE TABLE IF NOT EXISTS reconciliations '
                                '(id TEXT PRIMARY KEY,previous_result TEXT,state TEXT,result TEXT,observed REAL)')
                self.db.execute('INSERT OR IGNORE INTO reconciliations VALUES(?,?,?,?,?)',
                                (operation, old_result, a['state'], encode(result), time.time()))
                self.db.execute("UPDATE actions SET state=?,result=? WHERE id=? AND signature=? AND state='uncertain'",
                                (a['state'], encode(result), operation, signature))
        return not self.db.execute(
            "SELECT 1 FROM actions WHERE state='uncertain' AND resource=? AND id!=? LIMIT 1",
            (guard, exclude or '')).fetchone()

    def write_health(self, errors, pending):
        """Report execution progress separately from transport connectivity."""
        self.last_health = {'observed_at': time.time(), 'provider': self.remote.node,
                            'state': 'degraded' if errors else 'ready',
                            'pending': pending, 'blocked': errors}
        path = os.path.join(os.path.dirname(self.path), 'worker-health.json')
        temporary = path + '.' + uid()
        try:
            with open(temporary, 'w') as out:
                out.write(encode(self.last_health))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def fence(self, l):
        resource, gen, holder = l['resource'], l['generation'], l['id']
        guard = l.get('guard', resource)
        if resource not in self.physical:
            raise Fault('Unconfigured physical resource')
        self.reconcile_guard(guard)
        with self.db:
            row = self.db.execute(
                'SELECT gen,holder FROM fences WHERE resource=?', (guard,)).fetchone()
            if row and (row[0] > gen or (row[0] == gen and row[1] != holder)):
                raise Fault('Stale fence', 409)
            if self.db.execute("SELECT 1 FROM actions WHERE state='uncertain' AND resource=? LIMIT 1", (guard,)).fetchone():
                raise Fault('Reconcile uncertain hardware effect before a new fence', 409)
            self.db.execute('INSERT OR REPLACE INTO fences VALUES(?,?,?)', (guard, gen, holder))
        self.remote.call('cap.fenced', {'provider': self.remote.node,
                         'lease': holder, 'generation': gen})

    def configure(self, a, argv):
        if not isinstance(argv, list) or not argv or not os.path.isabs(argv[0]):
            raise Fault('Invalid setup argv')
        # Reconfiguration adapters must support repeated desired-state application.
        try:
            ret = subprocess.call(argv, timeout=self.timeout)
            info = {'exit': ret}
        except (OSError, subprocess.TimeoutExpired) as e:
            ret = -1
            info = {'error': str(e)}
        self.remote.call('cap.configured', {'provider': self.remote.node,
                         'activation': a['id'], 'success': ret == 0, 'evidence': info})

    def perform(self, a):
        args = {'provider': self.remote.node, 'operation': a['id'], 'generation': a['generation']}
        guard = a.get('guard', a['resource'])
        if not self.reconcile_guard(guard, exclude=a['id']):
            raise Fault('Reconcile uncertain operation in this slot before new work', 409)
        binding = self.db.execute(
            'SELECT gen,holder FROM fences WHERE resource=?', (guard,)).fetchone()
        if binding != (a['generation'], a['lease']):
            # Repair an old caller-acknowledged fence before claiming work.
            self.fence(dict(a, id=a['lease'], guard=guard))
        claimed = self.remote.call('provider.claim', args)
        if claimed['state'] != 'claimed':
            return
        sig = digest([a['contract'], a['inputs'], a['resource'], a['generation'], a['lease']])
        row = self.db.execute(
            'SELECT signature,state,result FROM actions WHERE id=?', (a['id'],)).fetchone()
        if row:
            if row[0] != sig:
                raise Fault('Operation identity conflict', 409)
            state, out = row[1], json.loads(row[2])
        else:
            adapter = next(
                (o for o in self.offers if o['contract'] == a['contract'] and o['resource'] == a['resource']), None)
            if not adapter:
                raise Fault('No local implementation')
            argv = adapter['argv']
            if not isinstance(argv, list) or not argv or not os.path.isabs(argv[0]):
                raise Fault('Expected absolute executable argv')
            out = {'reason': 'effect may have occurred; inspect resource before retry'}
            self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                            (a['id'], sig, 'uncertain', encode(out), guard))
            state = 'uncertain'
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout, stderr = proc.communicate(
                        encode({'operation': a['id'], 'inputs': a['inputs']}).encode(), timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise Fault('Adapter timed out; effect is uncertain')
                if proc.returncode:
                    out = {'error': 'adapter exit ' + \
                           str(proc.returncode), 'detail': stderr.decode('utf-8', 'replace')[-1024:]}
                else:
                    out = json.loads(stdout.decode())
                    from .api import validate
                    validate(claimed['output_schema'], out)
                    state = 'succeeded'
            except (OSError, ValueError, Fault) as exc:
                out = {'reason': str(exc)}
            self.db.execute('UPDATE actions SET state=?,result=? WHERE id=?',
                            (state, encode(out), a['id']))
        args.update(state=state, result=out)
        self.remote.call('provider.result', args)

    def once(self, pool=None):
        import logging
        queue = self.remote.call('provider.poll', {'provider': self.remote.node})
        errors, blocked = [], set()
        def failure(kind, item, error):
            guard = item.get('guard', item.get('resource', ''))
            if guard:
                blocked.add(guard)
            errors.append({'kind': kind, 'id': item['id'], 'guard': guard,
                           'error': type(error).__name__, 'reason': str(error)})
            logging.warning('Provider %s %s paused: %s', kind, item['id'], error)
        for a in queue.get('activations', []):
            try:
                found = next((o for o in self.offers if digest(
                    [self.remote.node, o['contract'], o['resource']]) == a['offer']), None)
                if found and found.get('configure_argv'):
                    self.configure(a, found['configure_argv'])
            except Exception as error:
                failure('activation', a, error)
        actions = [a for a in queue['actions']
                   if a.get('guard', a['resource']) not in blocked]
        if pool is not None:
            # Admission may need slow remote reconciliation. Isolate it by the
            # same fenced slot as execution rather than blocking the poller and
            # every unrelated slot while one uncertain effect is inspected.
            count = pool.dispatch(actions, fences=queue['fences'])
            errors.extend(getattr(pool, 'failures', {}).values())
        else:
            count = 0
            for lease in queue['fences']:
                try:
                    self.fence(lease)
                    count += 1
                except Exception as error:
                    failure('fence', lease, error)
            for a in actions:
                if a.get('guard', a['resource']) in blocked:
                    continue
                try:
                    self.perform(a)
                    count += 1
                except Exception as error:
                    failure('action', a, error)
        self.write_health(errors, len(queue['actions']))
        return count


class ActionPool:
    """A single poller and at most one active operation per fenced slot."""
    def __init__(self, worker, limit):
        from concurrent.futures import ThreadPoolExecutor
        self.worker = worker
        self.limit = limit
        self.executor = ThreadPoolExecutor(max_workers=limit)
        self.running = {}
        self.items = {}
        self.failures = {}

    def perform(self, action, fence=False):
        w = self.worker
        child = Worker(w.path, w.remote, w.offers, w.timeout)
        try:
            if fence:
                child.fence(action)
            else:
                child.perform(action)
        finally:
            child.db.close()

    def dispatch(self, actions, fences=()):
        import logging
        for guard, future in list(self.running.items()):
            if future.done():
                del self.running[guard]
                kind, identifier = self.items.pop(guard)
                if future.exception():
                    self.failures[guard] = {'kind': kind, 'id': identifier, 'guard': guard,
                                            'error': type(future.exception()).__name__, 'reason': str(future.exception())}
                    logging.warning('Provider %s %s paused: %s', kind, identifier, future.exception())
                else:
                    self.failures.pop(guard, None)
        started = 0
        # A pending fence takes precedence over an action for the same guard.
        # A guard has at most one future, preserving both fencing and execution
        # serialization while other guards can make progress independently.
        candidates = [('fence', lease) for lease in fences] + [('action', action) for action in actions]
        for kind, action in candidates:
            if len(self.running) >= self.limit:
                break
            guard = action.get('guard', action['resource'])
            if guard not in self.running:
                self.items[guard] = (kind, action['id'])
                self.running[guard] = self.executor.submit(self.perform, action, kind == 'fence')
                started += 1
        return started


def run_worker(directory, remote, cfg, once):
    import fcntl
    import logging
    from urllib.error import HTTPError
    with open(os.path.join(directory, 'worker.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Fault('Another adapter is already enforcing this node’s resources', 409)
        w = Worker(os.path.join(directory, 'worker.sqlite'),
                   remote, cfg['offers'], cfg.get('timeout', 30))
        parallelism = cfg.get('parallelism', 1)
        pool = ActionPool(w, parallelism) if parallelism > 1 else None
        try:
            announced = False
            while True:
                try:
                    if not announced:
                        w.advertise()
                        announced = True
                    count = w.once(pool)
                    if once:
                        return 0
                    time.sleep(.1 if count else cfg.get('poll_seconds', 2))
                except (OSError, ValueError, Fault, HTTPError) as err:
                    if once:
                        raise
                    logging.warning('Provider paused: %s', err)
                    time.sleep(cfg.get('retry_seconds', 5))
        finally:
            if pool is not None:
                pool.executor.shutdown(wait=True)
            w.db.close()


def reconcile_receipt(directory, remote, operation, state, outcome):
    """Operator reconciliation after inspecting a physical effect, preserving the original identity."""
    if state not in ('succeeded', 'failed') or not isinstance(outcome, dict):
        raise Fault('Invalid outcome')
    a = remote.call('cap.status', {'operation': operation})
    if a['provider'] != remote.node:
        raise Fault('Foreign operation', 403)
    db = sqlite3.connect(os.path.join(directory, 'worker.sqlite'))
    try:
        row = db.execute('SELECT signature,state,result FROM actions WHERE id=?',
                         (operation,)).fetchone()
        if not row or row[0] != digest([a['contract'], a['inputs'], a['resource'], a['generation'], a['lease']]):
            raise Fault('No matching local operation')
        if row[1] != 'uncertain' and (row[1] != state or json.loads(row[2]) != outcome):
            raise Fault('Conflicting result', 409)
        result = remote.call('provider.result', {'provider': remote.node, 'operation': operation,
                                                 'generation': a['generation'], 'state': state, 'result': outcome})
        db.execute('UPDATE actions SET state=?,result=? WHERE id=?',
                   (state, encode(outcome), operation))
        db.commit()
        return result
    finally:
        db.close()
