"""A hardware/service adapter with durable fences and a never-blind-replay ledger."""
import hashlib
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


RESULT_DEBUG_COUNT = 100
RESULT_DEBUG_BYTES = 1024 * 1024
RECONCILIATION_DEBUG_COUNT = 100
RECONCILIATION_DEBUG_BYTES = 1024 * 1024


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
    def __init__(self, url, site, node, key, ca=None, token=None, realm='site', controller=None):
        self.url, self.site, self.node, self.key = url, site, node, key
        self.ca, self.token, self.realm = ca, token, realm
        self.controller = controller

    def call(self, name, args, rid=None):
        payload = {'id': rid or uid(), 'operation': name, 'realm': self.realm,
                   'arguments': args, 'site': self.site}
        if self.controller is not None:
            payload['controller'] = self.controller
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
        self.db.executescript('CREATE TABLE IF NOT EXISTS result_retention_eligible(id TEXT PRIMARY KEY,signature TEXT);'
                              'CREATE TABLE IF NOT EXISTS result_acknowledged(id TEXT PRIMARY KEY);'
                              'CREATE TABLE IF NOT EXISTS historical_result_enrolled(id TEXT PRIMARY KEY,row_order INTEGER);'
                              'CREATE TABLE IF NOT EXISTS result_debug(seq INTEGER PRIMARY KEY AUTOINCREMENT,'
                              'id TEXT UNIQUE,state TEXT,result TEXT,bytes INTEGER);'
                              'CREATE TABLE IF NOT EXISTS result_cleanup(id TEXT PRIMARY KEY,argv TEXT,payload TEXT);'
                              'CREATE TABLE IF NOT EXISTS reconciliations '
                              '(id TEXT PRIMARY KEY,previous_result TEXT,state TEXT,result TEXT,observed REAL);'
                              'CREATE TABLE IF NOT EXISTS reconciliation_retention_eligible '
                              '(id TEXT PRIMARY KEY,signature TEXT,source TEXT,result_digest TEXT,compacted REAL);'
                              'CREATE TABLE IF NOT EXISTS reconciliation_debug '
                              '(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,'
                              'previous_result TEXT,result TEXT,bytes INTEGER);')
        # Bound health inventory to uncertain rows; never scan completed history.
        self.db.execute("CREATE INDEX IF NOT EXISTS worker_uncertain_health ON actions(id,resource) WHERE state='uncertain'")
        self._initialize_result_recovery()
        self.remote, self.offers, self.timeout = remote, offers, timeout
        self.physical = {x['resource'] for x in offers}
        # Pair this durable enforcement store with one authority namespace.
        self.db.execute('CREATE TABLE IF NOT EXISTS authority(id INTEGER PRIMARY KEY,value TEXT)')
        authority = encode([remote.site, remote.realm, remote.node])
        row = self.db.execute('SELECT value FROM authority WHERE id=1').fetchone()
        if row and row[0] != authority:
            raise Fault('Worker ledger belongs to another authority')
        self.db.execute('INSERT OR IGNORE INTO authority VALUES(1,?)', (authority,))
        from .supervised_commands import initialize
        initialize(self)

    def _initialize_result_recovery(self):
        # Disposable pending projection: never scan acknowledged history on a poll.
        # DDL stays inside executescript BEGIN for Python 3.4 compatibility.
        self.db.executescript("""BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS result_recovery_pending(id TEXT PRIMARY KEY,signature TEXT);
            CREATE TABLE IF NOT EXISTS result_recovery_schema(version INTEGER PRIMARY KEY);
            CREATE TRIGGER IF NOT EXISTS result_recovery_action_update AFTER UPDATE ON actions BEGIN
                DELETE FROM result_recovery_pending WHERE id=OLD.id;
                INSERT OR REPLACE INTO result_recovery_pending
                    SELECT NEW.id,NEW.signature FROM result_retention_eligible e
                    WHERE e.id=NEW.id AND e.signature=NEW.signature
                      AND NEW.state IN ('succeeded','failed')
                      AND NOT EXISTS(SELECT 1 FROM result_acknowledged k WHERE k.id=NEW.id);
            END;
            CREATE TRIGGER IF NOT EXISTS result_recovery_action_delete AFTER DELETE ON actions BEGIN
                DELETE FROM result_recovery_pending WHERE id=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS result_recovery_eligible_insert AFTER INSERT ON result_retention_eligible BEGIN
                INSERT OR REPLACE INTO result_recovery_pending
                    SELECT a.id,a.signature FROM actions a WHERE a.id=NEW.id AND a.signature=NEW.signature
                      AND a.state IN ('succeeded','failed')
                      AND NOT EXISTS(SELECT 1 FROM result_acknowledged k WHERE k.id=a.id);
            END;
            CREATE TRIGGER IF NOT EXISTS result_recovery_eligible_delete AFTER DELETE ON result_retention_eligible BEGIN
                DELETE FROM result_recovery_pending WHERE id=OLD.id;
            END;
            CREATE TRIGGER IF NOT EXISTS result_recovery_ack_insert AFTER INSERT ON result_acknowledged BEGIN
                DELETE FROM result_recovery_pending WHERE id=NEW.id;
            END;
        """)
        try:
            if not self.db.execute('SELECT 1 FROM result_recovery_schema WHERE version=1').fetchone():
                self.db.execute("INSERT OR IGNORE INTO result_recovery_pending "
                                "SELECT a.id,a.signature FROM result_retention_eligible e "
                                "CROSS JOIN actions a ON a.id=e.id AND a.signature=e.signature "
                                "WHERE a.state IN ('succeeded','failed') AND NOT EXISTS "
                                "(SELECT 1 FROM result_acknowledged k WHERE k.id=a.id)")
                self.db.execute('INSERT INTO result_recovery_schema VALUES(1)')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def advertise(self):
        for o in self.offers:
            args = {'provider': self.remote.node, 'contract': o['contract'], 'resource': o['resource'],
                    'service': o.get('service', ''), 'state': o.get('state', 'ready')}
            for field in ('capacity', 'usage', 'configuration', 'lease_class'):
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
            a = self.remote.call('cap.status', {'operation': operation, 'recovery': True})
            if (a.get('id') != operation or a.get('provider') != self.remote.node or
                    a.get('guard', a.get('resource')) != guard):
                raise Fault('Reconciliation binding mismatch', 409)
            identity = (a.get('identity_digest') if a.get('result_expired') is True else
                        digest([a['contract'], a['inputs'], a['resource'], a['generation'], a['lease']]))
            if signature != identity:
                raise Fault('Reconciliation identity mismatch', 409)
            result = a.get('recovery_result', {}) if a.get('result_expired') is True else a.get('result', {})
            if a.get('result_expired') is True and (not isinstance(result, dict) or
                    result.get('result_expired') is not True or
                    not isinstance(a.get('result_digest'), str) or
                    result.get('result_digest') != a['result_digest']):
                raise Fault('Invalid expired reconciliation summary', 409)
            if a.get('state') not in ('succeeded', 'failed') or not (
                    result.get('reconciliation', {}).get('quiescent') is True or
                    result.get('evidence', {}).get('quiescent') is True):
                continue
            self.db.execute('BEGIN IMMEDIATE')
            try:
                # The authority read happened outside this transaction. Do not
                # replace a receipt that another worker changed in the meantime.
                current = self.db.execute('SELECT signature,state,result FROM actions WHERE id=?',
                                          (operation,)).fetchone()
                if current != (signature, 'uncertain', old_result):
                    self.db.commit()
                    continue
                inserted = self.db.execute('INSERT OR IGNORE INTO reconciliations VALUES(?,?,?,?,?)',
                                           (operation, old_result, a['state'], encode(result), time.time()))
                if inserted.rowcount and self.db.execute(
                        'SELECT 1 FROM result_retention_eligible WHERE id=? AND signature=?',
                        (operation, signature)).fetchone():
                    # Never enroll pre-existing audit rows, even for an action
                    # born under the earlier action-result retention contract.
                    expired = a.get('result_expired') is True
                    self.db.execute('INSERT INTO reconciliation_retention_eligible VALUES(?,?,?,?,NULL)',
                                    (operation, signature,
                                     'controller_recovery' if expired else 'controller_result',
                                     a['result_digest'] if expired else digest(result)))
                self.db.execute("UPDATE actions SET state=?,result=? WHERE id=?",
                                (a['state'], encode(result), operation))
                if a.get('result_expired') is True:
                    # This is recovery evidence, not the original provider result.
                    self.db.execute('INSERT OR IGNORE INTO result_acknowledged VALUES(?)', (operation,))
                    self._compact_reconciliation(operation, signature)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return not self.db.execute(
            "SELECT 1 FROM actions WHERE state='uncertain' AND resource=? AND id!=? LIMIT 1",
            (guard, exclude or '')).fetchone()

    def historical_debt(self, active_guards=()):
        active=set(active_guards)
        # SQLite 3.8 may prefer the primary-key scan for ORDER BY; require
        # the bounded uncertainty index rather than scanning completed history.
        rows=list(self.db.execute("SELECT id,resource FROM actions INDEXED BY worker_uncertain_health WHERE state='uncertain' ORDER BY id LIMIT 65"))
        return {'operations':[{'id':op,'guard':guard} for op,guard in rows[:64] if guard not in active],
                'truncated':len(rows)>64}

    def write_health(self, errors, pending, historical_debt=None):
        """Report execution progress separately from transport connectivity."""
        self.last_health = {'observed_at': time.time(), 'provider': self.remote.node,
                            'state': 'degraded' if errors else 'ready',
                            'pending': pending, 'blocked': errors}
        if historical_debt is not None:
            self.last_health['historical_debt']=historical_debt
        path = os.path.join(os.path.dirname(self.path), 'worker-health.json')
        # One reusable staging file bounds future crash leftovers. A separate
        # stable lock serializes writers across processes; locking the staging
        # inode itself would be unsafe after another writer renames that inode.
        import fcntl
        import stat

        def check_file(name, fd=None):
            current = os.lstat(name)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise Fault('Unsafe worker health file', 409)
            if fd is not None:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise Fault('Worker health file changed during write', 409)

        def safe_open(name):
            # NONBLOCK also prevents a planted FIFO from stalling the worker.
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                check_file(name, fd)
                return fd
            except BaseException:
                os.close(fd)
                raise

        lock_path = path + '.write-v1.lock'
        temporary = path + '.pending-v1'
        with os.fdopen(safe_open(lock_path), 'a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            check_file(lock_path, lock.fileno())
            if os.path.lexists(path):
                check_file(path)
            with os.fdopen(safe_open(temporary), 'w') as out:
                os.ftruncate(out.fileno(), 0)
                out.write(encode(self.last_health))
                out.flush()
                os.fsync(out.fileno())
                check_file(temporary, out.fileno())
                os.replace(temporary, path)
            directory = os.open(os.path.dirname(self.path) or '.', os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

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
        from . import supervised_commands
        if supervised_commands.resume(self, a):
            return
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
            if self.db.execute('SELECT 1 FROM result_acknowledged WHERE id=?', (a['id'],)).fetchone():
                # The retained identity fence outlives its disposable response.
                return
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
            # Only actions born under this retention contract may lose payloads.
            # Both records must be durable before the adapter can cause effects.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                                (a['id'], sig, 'uncertain', encode(out), guard))
                self.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', (a['id'], sig))
                if supervised_commands.supported(a):
                    self.db.execute('INSERT INTO supervised_pending VALUES(?,?,?,?,?)',
                                    (a['id'], sig, encode(a), encode(adapter), encode(claimed['output_schema'])))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            if supervised_commands.supported(a):
                supervised_commands.finish(self, a, adapter, claimed['output_schema'], sig)
                return
            state = 'uncertain'
            request = {'operation': a['id'], 'inputs': a['inputs']}
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    request = {'operation': a['id'], 'inputs': a['inputs']}
                    if adapter.get('configuration', {}).get('lease_class') == 'long_command' or adapter.get('lease_class') == 'long_command':
                        from .command_watch import communicate
                        request['progress_path'] = os.path.join(os.path.dirname(self.path), 'progress-' + digest(a['id']) + '.json')
                        stdout, stderr = communicate(self, proc, request, a, adapter)
                    else:
                        stdout, stderr = proc.communicate(encode(request).encode(), timeout=self.timeout)
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
                    state = 'failed' if out.get('evidence', {}).get('reason') == 'stop_requested' else 'succeeded'
            except (OSError, ValueError, Fault) as exc:
                out = {'reason': str(exc)}
            for path in (request.get('progress_path', ''), request.get('progress_path', '') + '.stop'):
                if path and os.path.exists(path):
                    os.unlink(path)
            self.db.execute('UPDATE actions SET state=?,result=? WHERE id=?',
                            (state, encode(out), a['id']))
        args.update(state=state, result=out)
        acknowledged = self.remote.call('provider.result', args)
        if (state in ('succeeded', 'failed') and isinstance(acknowledged, dict) and
                all(acknowledged.get(k) == v for k, v in (
                    ('id', a['id']), ('provider', self.remote.node),
                    ('generation', a['generation']), ('state', state))) and
                (acknowledged.get('result') == out or
                 (acknowledged.get('result_expired') is True and
                  acknowledged.get('identity_digest') == sig and
                  acknowledged.get('result_digest') == digest(out)))):
            adapter = next((o for o in self.offers if o['contract'] == a['contract']
                            and o['resource'] == a['resource']), {})
            if self.retire_result(a['id'], sig, state, out, adapter.get('acknowledge_argv')):
                self.cleanup_results(a['id'])

    def _prune_debug(self, table, keep, budget):
        # Callers supply fixed internal table names. These tables contain only
        # new disposable copies, never legacy or pending-recovery payloads.
        count, used = 0, 0
        for seq, amount in self.db.execute('SELECT seq,bytes FROM ' + table + ' ORDER BY seq DESC').fetchall():
            count += 1
            used += amount
            if count > keep or used > budget:
                self.db.execute('DELETE FROM ' + table + ' WHERE seq=?', (seq,))

    def _compact_reconciliation(self, operation, signature, debug_seq=None):
        """Inside the ACK transaction, retire only a newly enrolled audit copy."""
        row = self.db.execute(
            "SELECT r.previous_result,r.result FROM reconciliation_retention_eligible e "
            "CROSS JOIN reconciliations r ON r.id=e.id "
            "CROSS JOIN actions a ON a.id=e.id AND a.signature=e.signature "
            "CROSS JOIN result_retention_eligible t ON t.id=a.id AND t.signature=a.signature "
            "CROSS JOIN result_acknowledged k ON k.id=a.id "
            "WHERE e.id=? AND e.signature=? AND e.compacted IS NULL "
            "AND a.state IN ('succeeded','failed') AND a.state=r.state AND a.result=r.result",
            (operation, signature)).fetchone()
        if not row:
            return
        sizes = [len(body.encode('utf-8')) for body in row]
        if sum(sizes) <= RECONCILIATION_DEBUG_BYTES:
            self.db.execute('INSERT INTO reconciliation_debug(seq,id,previous_result,result,bytes) VALUES(?,?,?,?,?)',
                            (debug_seq, operation, row[0], row[1], sum(sizes)))
        summaries = [encode({'payload_expired': True, 'payload_bytes': size,
                             'payload_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()})
                     for body, size in zip(row, sizes)]
        self.db.execute('UPDATE reconciliations SET previous_result=?,result=? WHERE id=?',
                        (summaries[0], summaries[1], operation))
        self.db.execute('UPDATE reconciliation_retention_eligible SET compacted=? WHERE id=?',
                        (time.time(), operation))
        self._prune_debug('reconciliation_debug', RECONCILIATION_DEBUG_COUNT, RECONCILIATION_DEBUG_BYTES)

    def retire_result(self, operation, signature, state, result, acknowledge_argv=None, legacy=None):
        """Roll off only terminal results acknowledged by their authority."""
        body = encode(result)
        size = len(body.encode('utf-8'))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if legacy is not None:
                from .worker_history import enroll
                enroll(self, operation, signature, state, body, legacy)
            if not self.db.execute('SELECT 1 FROM result_retention_eligible WHERE id=? AND signature=?',
                                   (operation, signature)).fetchone():
                # A successful re-delivery does not authorize purging old data.
                self.db.commit()
                return False
            row = self.db.execute('SELECT signature,state,result FROM actions WHERE id=?',
                                  (operation,)).fetchone()
            if row != (signature, state, body) or state not in ('succeeded', 'failed'):
                raise Fault('Local result changed before acknowledgement', 409)
            if size <= RESULT_DEBUG_BYTES:
                self.db.execute('INSERT OR REPLACE INTO result_debug(seq,id,state,result,bytes) VALUES(?,?,?,?,?)',
                                (legacy['debug_seq'] if legacy else None, operation, state, body, size))
            if acknowledge_argv is not None:
                if (not isinstance(acknowledge_argv, list) or not acknowledge_argv or
                        not all(isinstance(arg, str) for arg in acknowledge_argv) or
                        not os.path.isabs(acknowledge_argv[0])):
                    raise Fault('Expected absolute acknowledgement executable argv')
                self.db.execute('INSERT OR IGNORE INTO result_cleanup VALUES(?,?,?)',
                                (operation, encode(acknowledge_argv),
                                 encode({'operation': operation, 'result_digest': digest(result)})))
            self.db.execute('INSERT OR IGNORE INTO result_acknowledged VALUES(?)', (operation,))
            self._compact_reconciliation(operation, signature, legacy['debug_seq'] if legacy else None)
            self.db.execute("UPDATE actions SET result='{}' WHERE id=?", (operation,))
            self._prune_debug('result_debug', RESULT_DEBUG_COUNT, RESULT_DEBUG_BYTES)
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def cleanup_results(self, operation=None):
        """Retry acknowledged, idempotent adapter cleanup without replaying effects."""
        query = ('SELECT c.id,c.argv,c.payload FROM result_cleanup c '
                 'CROSS JOIN result_retention_eligible e ON e.id=c.id '
                 'CROSS JOIN actions a ON a.id=e.id AND a.signature=e.signature')
        rows = self.db.execute(query + (' WHERE c.id=?' if operation else ' ORDER BY c.id LIMIT 8'),
                               (operation,) if operation else ()).fetchall()
        for identifier, argv, payload in rows:
            proc = subprocess.Popen(json.loads(argv), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.communicate(payload.encode('utf-8'),
                                 timeout=30 if self.timeout is None else min(self.timeout, 30))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise Fault('Acknowledged result cleanup timed out; cleanup remains pending', 503)
            if proc.returncode:
                raise Fault('Acknowledged result cleanup failed; cleanup remains pending', 503)
            self.db.execute('DELETE FROM result_cleanup WHERE id=? AND argv=? AND payload=?',
                            (identifier, argv, payload))

    def recover_result_acknowledgements(self, limit=8):
        """Confirm bounded new terminal receipts whose delivery ACK was lost."""
        import logging
        after = getattr(self, '_result_recovery_after', '')
        query = ("SELECT a.id,a.signature,a.state,a.result FROM result_recovery_pending p "
                 "CROSS JOIN actions a ON a.id=p.id AND a.signature=p.signature "
                 "WHERE p.id>? AND NOT EXISTS (SELECT 1 FROM supervised_pending s WHERE s.id=p.id) ORDER BY p.id LIMIT ?")
        limit = max(0, min(8, limit))
        rows = self.db.execute(query, (after, limit)).fetchall()
        if not rows and after:
            rows = self.db.execute(query, ('', limit)).fetchall()
        for operation, signature, state, body in rows:
            self._result_recovery_after = operation
            try:
                action = self.remote.call('cap.status', {'operation': operation, 'recovery': True})
                if (action.get('id') != operation or action.get('provider') != self.remote.node or
                        action.get('state') != state):
                    continue
                expired = action.get('result_expired') is True
                identity = (action.get('identity_digest') if expired else
                            digest([action[k] for k in ('contract', 'inputs', 'resource', 'generation', 'lease')]))
                result = json.loads(body)
                confirmed = action.get('result_digest') if expired else digest(action.get('result'))
                if identity != signature or confirmed != digest(result):
                    continue
                adapter = next((o for o in self.offers if o['contract'] == action.get('contract')
                                and o['resource'] == action.get('resource')), {})
                if self.retire_result(operation, signature, state, result, adapter.get('acknowledge_argv')):
                    self.cleanup_results(operation)
            except Exception as error:
                # A failing receipt must not starve other receipts or execution.
                logging.warning('Result acknowledgement recovery pending: %s', type(error).__name__)

    def once(self, pool=None):
        import logging
        try:
            self.cleanup_results()
        except Exception as error:
            # A filesystem cleanup failure cannot requeue the physical effect.
            logging.warning('Acknowledged result cleanup pending: %s', type(error).__name__)
        from .supervised_commands import cleanup_expired
        try:
            cleanup_expired(self)
        except Exception as error:
            logging.warning('Runner receipt expiry pending: %s', type(error).__name__)
        self.recover_result_acknowledgements()
        queue = self.remote.call('provider.poll', {'provider': self.remote.node})
        try:
            from .worker_expiry import expire
            page=expire(self,after=getattr(self,'_whole_expiry_after',''),inventory=queue)
            self._whole_expiry_after=page['after'] if page['more'] else ''
        except Exception as error:
            logging.warning('Completed record expiry pending: %s',type(error).__name__)
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
        if callable(getattr(self, 'stream_handler', None)):
            self.stream_handler(queue.get('streams', []))
        from .supervised_commands import pending
        recovering = pending(self)
        recovery_ids = {a['id'] for a in recovering}
        recovery_guards = {a.get('guard', a['resource']) for a in recovering}
        # Resolve old custody before admitting a newer fence on the same slot.
        queue['fences'] = [l for l in queue['fences'] if l.get('guard', l['resource']) not in recovery_guards]
        queue['actions'] = recovering + [a for a in queue['actions'] if a['id'] not in recovery_ids
                                        and a.get('guard', a['resource']) not in recovery_guards]
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
        active_guards={item.get('guard',item.get('resource','')) for item in queue['actions']+queue['fences']}
        if pool is not None:active_guards.update(getattr(pool,'running',{}))
        self.write_health(errors, len(queue['actions']), self.historical_debt(active_guards))
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
                kind, identifier, generation = self.items.pop(guard)
                if future.exception():
                    self.failures[guard] = {'kind': kind, 'id': identifier, 'generation': generation, 'guard': guard,
                                            'error': type(future.exception()).__name__, 'reason': str(future.exception())}
                    logging.warning('Provider %s %s paused: %s', kind, identifier, future.exception())
                else:
                    self.failures.pop(guard, None)
        started = 0
        # A pending fence takes precedence over an action for the same guard.
        # A guard has at most one future, preserving both fencing and execution
        # serialization while other guards can make progress independently.
        candidates = [('fence', lease) for lease in fences] + [('action', action) for action in actions]
        outstanding={(item.get('guard',item['resource']),kind,item['id'],item.get('generation')) for kind,item in candidates}
        # Failures describe current admission/work, not a permanent health latch.
        # Removing this reporting entry never changes ledger state or fences.
        for guard, failure in list(self.failures.items()):
            identity=(guard,failure['kind'],failure['id'],failure.get('generation'))
            running=self.items.get(guard)
            if identity not in outstanding and (running is None or (guard,)+running!=identity):
                del self.failures[guard]
        for kind, action in candidates:
            if len(self.running) >= self.limit:
                break
            guard = action.get('guard', action['resource'])
            if guard not in self.running:
                self.items[guard] = (kind, action['id'], action.get('generation'))
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
    a = remote.call('cap.status', {'operation': operation, 'recovery': True})
    if a.get('id') != operation or a.get('provider') != remote.node:
        raise Fault('Foreign operation', 403)
    db = sqlite3.connect(os.path.join(directory, 'worker.sqlite'), isolation_level=None, timeout=30)
    try:
        # Python 3.4 DDL may commit implicitly. Initialize before any DML.
        db.execute('CREATE TABLE IF NOT EXISTS result_acknowledged(id TEXT PRIMARY KEY)')
        row = db.execute('SELECT signature,state,result FROM actions WHERE id=?',
                         (operation,)).fetchone()
        identity = (a.get('identity_digest') if a.get('result_expired') is True else
                    digest([a['contract'], a['inputs'], a['resource'], a['generation'], a['lease']]))
        if not row or row[0] != identity:
            raise Fault('No matching local operation')

        def confirms(record):
            return (isinstance(record, dict) and all(record.get(k) == v for k, v in (
                    ('id', operation), ('provider', remote.node),
                    ('generation', a['generation']), ('state', state))) and
                    ((record.get('result_expired') is not True and record.get('result') == outcome) or
                     (record.get('result_expired') is True and
                      record.get('identity_digest') == identity and
                      record.get('result_digest') == digest(outcome))))

        acknowledged = db.execute('SELECT 1 FROM result_acknowledged WHERE id=?', (operation,)).fetchone()
        if acknowledged:
            # An exact operator retry must neither submit a cleared result nor
            # rehydrate an already acknowledged payload outside recovery polling.
            if row[1] != state or not confirms(a):
                raise Fault('Conflicting acknowledged result', 409)
            return a
        if row[1] != 'uncertain' and (row[1] != state or json.loads(row[2]) != outcome):
            raise Fault('Conflicting result', 409)
        if a.get('result_expired') is True:
            if not confirms(a):
                raise Fault('Conflicting expired result', 409)
            # Never submit a recovery summary as original provider output.
            result = a
        else:
            result = remote.call('provider.result', {'provider': remote.node, 'operation': operation,
                                                     'generation': a['generation'], 'state': state, 'result': outcome})
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT signature,state,result FROM actions WHERE id=?', (operation,)).fetchone()
        if current != row:
            if (current and current[:2] == (identity, state) and confirms(result) and
                    db.execute('SELECT 1 FROM result_acknowledged WHERE id=?', (operation,)).fetchone()):
                db.commit()
                return result
            raise Fault('Local result changed during reconciliation', 409)
        db.execute('UPDATE actions SET state=?,result=? WHERE id=?',
                   (state, encode(outcome), operation))
        if a.get('result_expired') is True:
            marked = (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                 "AND name='result_retention_eligible'").fetchone() and
                      db.execute('SELECT 1 FROM result_retention_eligible WHERE id=? AND signature=?',
                                 (operation, identity)).fetchone())
            if not marked:
                db.execute('INSERT OR IGNORE INTO result_acknowledged VALUES(?)', (operation,))
            # Marked full outcomes remain pending until the worker can atomically
            # retire them with its configured adapter cleanup hook. Inserting an
            # ACK here would bypass that recovery projection and strand outputs.
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
