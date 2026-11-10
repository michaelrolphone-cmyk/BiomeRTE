"""Worker integration for durable terminal/Docker runners and result outboxes."""
import json
import os
import time
from . import command_runner as runner
from .store import Fault, digest, encode

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
    return [json.loads(row[0]) for row in worker.db.execute(
        'SELECT action FROM supervised_pending ORDER BY id')]


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
        next_report, last_progress = 0, None
        while observation['state'] in ('starting', 'launching', 'running'):
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + 10
                try:
                    control = worker.remote.call('cap.command.state', args)
                    if control.get('kill_requested') or control.get('state') == 'abandoned':
                        runner.save(request['progress_path'] + '.stop', {'stop': True})
                    else:
                        try:
                            worker.remote.call('provider.custody', dict(args, observation=dict(
                                observation, signature=signature, recovery_path=directory,
                                local_observed_at=observation.get('observed_at'))))
                        except Exception:
                            worker.remote.call('cap.heartbeat', args)
                    if os.path.exists(request['progress_path']):
                        progress = runner.read(request['progress_path'])
                        if progress.get('signature') != last_progress:
                            worker.remote.call('agent.progress', dict(args, checkpoint=progress))
                            last_progress = progress.get('signature')
                except Exception:
                    # Network loss does not discard custody or the completion record.
                    pass
            time.sleep(.2)
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
        elif observation['state'] == 'finished' and observation['returncode'] == 0:
            try:
                with open(os.path.join(directory, 'stdout')) as source:
                    out = json.load(source)
                from .api import validate
                validate(schema, out)
                state = 'failed' if (out.get('evidence', {}).get('reason') == 'stop_requested' or
                                     os.path.exists(request['progress_path'] + '.stop')) else 'succeeded'
            except (OSError, ValueError, Fault) as error:
                out = {'reason': str(error), 'runner': observation}
        else:
            out = {'reason': observation.get('reason', 'Adapter did not return a valid result'),
                   'runner': observation, 'replay_allowed': False,
                   'recovery_path': directory}
        worker.db.execute('UPDATE actions SET state=?,result=? WHERE id=? AND signature=?',
                          (state, encode(out), action['id'], signature))
    args.update(state=state, result=out)
    acknowledged = worker.remote.call('provider.result', args)
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
        for name in ('input.json', 'request.json', 'stdout', 'stderr', 'runner.log', 'progress.json', 'progress.json.stop'):
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
                         'custody.lock','acknowledged.json')
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
