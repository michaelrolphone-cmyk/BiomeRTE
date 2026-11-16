"""Worker integration for durable terminal/Docker runners and result outboxes."""
import json
import os
import time
from . import command_runner as runner
from .store import Fault, digest, encode
from .command_watch import OutputLiveness

CONTRACTS = ('host.terminal.run:1', 'docker.cli:1')


def initialize(worker):
    worker.db.execute('CREATE TABLE IF NOT EXISTS supervised_pending '
                      '(id TEXT PRIMARY KEY, signature TEXT, action TEXT, adapter TEXT, schema TEXT)')
    worker.db.execute('CREATE TABLE IF NOT EXISTS supervised_delivery '
                      '(id TEXT PRIMARY KEY, signature TEXT, state TEXT, result_digest TEXT)')
    worker.db.execute('CREATE TABLE IF NOT EXISTS supervised_expiry(id TEXT PRIMARY KEY)')
    worker.db.execute('CREATE TRIGGER IF NOT EXISTS supervised_delivery_expiry AFTER DELETE ON actions '
                      'BEGIN INSERT OR IGNORE INTO supervised_expiry SELECT OLD.id '
                      'WHERE EXISTS(SELECT 1 FROM supervised_delivery WHERE id=OLD.id); '
                      'DELETE FROM supervised_delivery WHERE id=OLD.id; END')


def pending(worker):
    initialize(worker)
    # This is a compact active index, not a scan of historical actions.
    return [action for row in worker.db.execute(
        'SELECT action FROM supervised_pending ORDER BY id')
        for action in [json.loads(row[0])] if not action.get('delivery_unresolved')]


def unresolved(worker, limit=32):
    """Bounded inspection of parked outboxes; no ACK or evidence deletion."""
    if type(limit) is not int or not 1 <= limit <= 64:
        raise Fault('Invalid unresolved delivery page size')
    initialize(worker)
    items = []
    for row in worker.db.execute('SELECT action FROM supervised_pending ORDER BY id'):
        action = json.loads(row[0])
        if action.get('delivery_unresolved'):
            items.append(dict(id=action['id'], **action['delivery_unresolved']))
            if len(items) == limit:
                break
    return items


def supported(action):
    return action['contract'] in CONTRACTS


def resume(worker, action):
    initialize(worker)
    row = worker.db.execute('SELECT signature,action,adapter,schema FROM supervised_pending WHERE id=?',
                            (action['id'],)).fetchone()
    if not row:
        return False
    expected = digest([action['contract'], action['inputs'], action['resource'],
                       action['generation'], action['lease']])
    if row[0] != expected:
        raise Fault('Runner recovery identity conflict', 409)
    current = worker.db.execute('SELECT state FROM actions WHERE id=?', (action['id'],)).fetchone()
    if current and current[0] == 'uncertain':
        try:
            authority = worker.remote.call('cap.status', {'operation': action['id'], 'recovery': True})
        except Exception:
            authority = {}
        if authority.get('state') in ('succeeded', 'failed'):
            worker.reconcile_guard(action.get('guard', action['resource']))
            repaired = worker.db.execute('SELECT state FROM actions WHERE id=?', (action['id'],)).fetchone()
            if repaired and repaired[0] == authority['state']:
                # Exact identity and quiescence were checked by reconcile_guard.
                # An expired recovery summary must never replace the original result.
                result_digest = (authority['result_digest'] if authority.get('result_expired') is True
                                 else digest(authority['result']))
                worker.db.execute('INSERT OR REPLACE INTO supervised_delivery VALUES(?,?,?,?)',
                                  (action['id'], row[0], authority['state'], result_digest))
    finish(worker, json.loads(row[1]), json.loads(row[2]), json.loads(row[3]), row[0])
    return True


def start(worker, action, adapter, schema, signature):
    initialize(worker)
    worker.db.execute('INSERT INTO supervised_pending VALUES(?,?,?,?,?)',
                      (action['id'], signature, encode(action), encode(adapter), encode(schema)))
    finish(worker, action, adapter, schema, signature)


def finish(worker, action, adapter, schema, signature):
    root = os.path.join(os.path.dirname(worker.path), 'command-runners')
    directory = runner.location(root, action['id'])
    request = {'operation': action['id'], 'inputs': action['inputs'],
               'progress_path': os.path.join(directory, 'progress.json')}
    local = worker.db.execute('SELECT signature,state,result FROM actions WHERE id=?',
                              (action['id'],)).fetchone()
    if not local or local[0] != signature:
        raise Fault('Missing exact runner execution record', 409)
    args = dict(provider=worker.remote.node, operation=action['id'], generation=action['generation'])
    delivered = worker.db.execute('SELECT signature,state,result_digest FROM supervised_delivery WHERE id=?',
                                  (action['id'],)).fetchone()
    if delivered:
        # ACK was durable before cleanup. Never relaunch or republish a retired result.
        if delivered[0] != signature:
            raise Fault('Runner delivery identity conflict', 409)
        cleanup(worker, action, adapter, signature)
        return
    state, out = local[1], json.loads(local[2])
    if state == 'uncertain':
        observation = runner.ensure(root, action['id'], signature, adapter['argv'], request, worker.timeout)
        next_report = 0
        liveness = OutputLiveness()
        interval = min(10.0, adapter.get('configuration', {}).get('heartbeat_interval_seconds', 60) / 2.0)
        while observation['state'] in ('starting', 'launching', 'running'):
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + interval
                try:
                    control = worker.remote.call('cap.command.state', args)
                    if control.get('kill_requested') or control.get('state') == 'abandoned':
                        runner.save(request['progress_path'] + '.stop', {'stop': True})
                        stop_native_terminal(worker, action)
                    else:
                        try:
                            worker.remote.call('provider.custody', dict(args, observation=dict(
                                observation, signature=signature, recovery_path=directory,
                                local_observed_at=observation.get('observed_at'))))
                        except Exception:
                            worker.remote.call('cap.heartbeat', args)
                    progress = runner.read(request['progress_path']) if os.path.exists(request['progress_path']) else {}
                    changed, checkpoint = liveness.observe(progress)
                    if changed:
                        worker.remote.call('agent.progress', dict(args, checkpoint=checkpoint))
                except Exception:
                    # Network loss does not discard custody or the completion record.
                    pass
            time.sleep(min(.2, interval))
            observation = runner.inspect(directory)
        try:
            worker.remote.call('provider.custody', dict(args, observation=dict(
                observation, signature=signature, recovery_path=directory,
                local_observed_at=observation.get('observed_at'))))
        except Exception:
            pass
        if observation['state'] == 'not_started':
            state = 'failed'
            out = {'reason': observation['reason'], 'evidence': {'executed': False, 'quiescent': True}}
        elif (observation['state'] == 'finished' and action['contract'] == 'docker.cli:1'
              and os.path.exists(os.path.join(directory,'docker-completion.json'))):
            out = recover_docker_result(directory, action, worker.remote)
            from .api import validate
            validate(schema,out)
            state = 'failed' if os.path.exists(request['progress_path']+'.stop') else 'succeeded'
        elif observation['state'] == 'finished' and observation['returncode'] == 0:
            try:
                with open(os.path.join(directory, 'stdout')) as source:
                    out = json.load(source)
                from .api import validate
                validate(schema, out)
                state = 'failed' if (out.get('evidence', {}).get('reason') == 'stop_requested' or
                                     (action['contract'] == 'docker.cli:1' and
                                      os.path.exists(request['progress_path'] + '.stop'))) else 'succeeded'
            except (OSError, ValueError, Fault) as error:
                out = {'reason': str(error), 'runner': observation}
        else:
            out = {'reason': observation.get('reason', 'Adapter did not return a valid result'),
                   'runner': observation, 'replay_allowed': False,
                   'recovery_path': directory}
        worker.db.execute('UPDATE actions SET state=?,result=? WHERE id=? AND signature=?',
                          (state, encode(out), action['id'], signature))
    args.update(state=state, result=out)
    try:
        acknowledged = worker.remote.call('provider.result', args)
    except Exception:
        # Only a successful authoritative read can park a missing receipt.
        # Unreachable transport remains retryable; neither case permits replay.
        acknowledged = worker.remote.call('cap.status', {'operation': action['id'], 'recovery': True})
        if (isinstance(acknowledged, dict) and acknowledged.get('id') == action['id'] and
                acknowledged.get('state') == 'missing_unknown' and acknowledged.get('acknowledged') is False):
            parked = dict(action, delivery_unresolved=dict(state='authoritative_receipt_missing',
                          observed_at=time.time(), acknowledged=False, replay_allowed=False,
                          recovery_path=directory, next_action='Reconcile original executor evidence; retained output is not acknowledged'))
            worker.db.execute('UPDATE supervised_pending SET action=? WHERE id=? AND signature=?',
                              (encode(parked), action['id'], signature))
            return
        if not isinstance(acknowledged, dict) or acknowledged.get('result_expired') is not True:
            raise
    exact = isinstance(acknowledged, dict) and all(acknowledged.get(k) == v for k, v in (
        ('id', action['id']), ('provider', worker.remote.node), ('generation', action['generation'])))
    outcome_matches = exact and (acknowledged.get('state') == state or
        (acknowledged.get('state') == 'abandoned' and acknowledged.get('execution_state') == state))
    result_matches = exact and (acknowledged.get('result') == out or (
        acknowledged.get('result_expired') is True and acknowledged.get('identity_digest') == signature
        and acknowledged.get('result_digest') == digest(out)))
    if state in ('succeeded', 'failed') and outcome_matches and result_matches:
        worker.db.execute('INSERT OR REPLACE INTO supervised_delivery VALUES(?,?,?,?)',
                          (action['id'], signature, state, digest(out)))
        cleanup(worker, action, adapter, signature)
    elif state == 'uncertain':
        # Retain evidence and ownership, but don't busy-loop a known lost custodian.
        raise Fault('Runner requires reconciliation: ' + directory, 409)
    else:
        raise Fault('Runner result acknowledgement mismatch', 409)


def cleanup(worker, action, adapter, signature):
    row = worker.db.execute('SELECT state,result FROM actions WHERE id=?', (action['id'],)).fetchone()
    already = worker.db.execute('SELECT 1 FROM result_acknowledged WHERE id=?', (action['id'],)).fetchone()
    if row and row[0] in ('succeeded', 'failed') and not already:
        if not worker.retire_result(action['id'], signature, row[0], json.loads(row[1]),
                                    adapter.get('acknowledge_argv')):
            return
    worker.cleanup_results(action['id'])
    # Retire bulky runner copies only after the exact controller ACK is durable.
    directory = runner.location(os.path.join(os.path.dirname(worker.path), 'command-runners'), action['id'])
    receipt = worker.db.execute('SELECT signature,state,result_digest FROM supervised_delivery WHERE id=?',
                                (action['id'],)).fetchone()
    if os.path.isdir(directory):
        runner.save(os.path.join(directory, 'acknowledged.json'),
                    dict(signature=receipt[0], state=receipt[1], result_digest=receipt[2]))
        for name in ('input.json', 'request.json', 'stdout', 'stderr', 'runner.log', 'progress.json', 'progress.json.stop','docker-completion.json','docker.stdout','docker.stderr'):
            path = os.path.join(directory, name)
            if os.path.exists(path):
                os.unlink(path)
    worker.db.execute('DELETE FROM supervised_pending WHERE id=? AND signature=?',
                      (action['id'], signature))


def cleanup_expired(worker, limit=8):
    """Remove only acknowledged runner files after authoritative action expiry."""
    import stat
    for operation, in worker.db.execute('SELECT id FROM supervised_expiry ORDER BY id LIMIT ?', (limit,)).fetchall():
        directory = runner.location(os.path.join(os.path.dirname(worker.path), 'command-runners'), operation)
        if os.path.lexists(directory):
            if (os.path.islink(directory) or os.path.islink(os.path.dirname(directory)) or
                    os.path.realpath(directory) != os.path.join(os.path.realpath(os.path.dirname(directory)),
                                                               os.path.basename(directory))):
                raise Fault('Unsafe runner expiry directory')
            if not os.path.exists(os.path.join(directory, 'acknowledged.json')):
                raise Fault('Runner expiry has no durable acknowledgement')
            handle = runner.locked(os.path.join(directory, 'custody.lock'))
            if handle is None:
                continue
            with handle:
                names = ('input.json','request.json','stdout','stderr','runner.log','progress.json',
                         'progress.json.stop','completion.json','state.json','prepare.lock',
                         'custody.lock','acknowledged.json','docker-completion.json','docker.stdout','docker.stderr')
                for name in names:
                    path = os.path.join(directory, name)
                    if os.path.lexists(path):
                        info = os.lstat(path)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                            raise Fault('Unsafe runner expiry file')
                for name in names:
                    path = os.path.join(directory, name)
                    if os.path.exists(path):
                        os.unlink(path)
                if not os.listdir(directory):
                    os.rmdir(directory)
        worker.db.execute('DELETE FROM supervised_expiry WHERE id=?', (operation,))


def stop_native_terminal(worker, action):
    """Forward explicit stop to the existing Pi custodian, never a new execution."""
    if action['contract'] != 'host.terminal.run:1':
        return
    import sqlite3
    path=os.path.join(os.path.dirname(worker.path),'terminal.sqlite')
    if not os.path.exists(path):
        return
    db=sqlite3.connect(path,timeout=10)
    try:
        row=db.execute('SELECT body,state FROM jobs WHERE id=?',(action['id'],)).fetchone()
        if not row or row[1] not in ('queued','launching','running'):
            return
        body=json.loads(row[0])
        expected=action['inputs']['argv']
        actual=body['argv']
        if (actual[1:] != expected[1:] or
                os.path.realpath(actual[0]) != os.path.realpath(expected[0])):
            raise Fault('Native terminal stop identity mismatch',409)
        with db:
            db.execute("UPDATE jobs SET state='stop_requested' WHERE id=? AND state IN ('queued','launching','running')",
                       (action['id'],))
    finally:
        db.close()


def recover_docker_result(directory, action, remote):
    """Deliver exact retained CLI completion, without invoking any executable."""
    import base64,hashlib,stat,uuid
    receipt=runner.read(os.path.join(directory,'docker-completion.json'))
    if receipt.get('input_digest') != digest(action['inputs']):
        raise Fault('Docker retained completion identity mismatch',409)
    result=dict(receipt['result']);result['streams']={}
    if result.get('evidence',{}).get('quiescent') is not True:
        raise Fault('Docker retained completion is not quiescent',409)
    for name in ('stdout','stderr'):
        path=os.path.join(directory,'docker.'+name)
        info=os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise Fault('Unsafe retained Docker output')
        meta=receipt['streams'][name]
        with open(path,'rb') as source:
            whole=hashlib.sha256()
            for block in iter(lambda:source.read(32768),b''):whole.update(block)
            if whole.hexdigest()!=meta['sha256']:raise Fault('Retained Docker output digest mismatch',409)
            source.seek(0);chunks=[];preview=b'';size=0
            for block in iter(lambda:source.read(32768),b''):
                if not chunks:preview=block[:4096]
                expected={'sha256':hashlib.sha256(block).hexdigest(),'size':len(block)}
                ack=remote.call('artifact.put',{'data':base64.b64encode(block).decode('ascii'),'transient':True},
                                'docker-retained-chunk-'+str(uuid.uuid4()))
                if ack != expected:raise Fault('Docker artifact receipt mismatch',409)
                chunks.append(ack);size+=len(block)
        result[name]=preview.decode('utf8','replace')
        result['streams'][name]=dict(sha256=meta['sha256'],size=size,chunks=chunks,
                                    preview_truncated=size>len(preview),
                                    total_bytes=meta['total_bytes'],truncated=meta['total_bytes']>size)
    result.pop('evidence',None)
    return result

