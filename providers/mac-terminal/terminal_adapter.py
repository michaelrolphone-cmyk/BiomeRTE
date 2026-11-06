"""Mac terminal execution with durable output receipts before transport."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

from mac_provider import ROOT, Remote
from terminal import check_argv, store_stream
from terminal_limits import apply_limits, read_limits


def save_receipt(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def execute(request, cfg, remote):
    profile = cfg['terminal_profile']
    try:
        argv = check_argv(request['inputs']['argv'], profile)
    except ValueError as exc:
        return {'exit_code': 126, 'stdout': '', 'stderr': str(exc), 'streams': {},
                'evidence': {'executed': False, 'quiescent': True, 'reason': 'argv_rejected'}}
    operation = request['operation']
    signature = hashlib.sha256(json.dumps([operation, request['inputs'], profile],
                                           sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    directory = ROOT / 'terminal-results' / hashlib.sha256(operation.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = directory / 'receipt.json'
        saved = json.loads(receipt.read_text()) if receipt.exists() else None
        if saved and saved['signature'] != signature:
            raise RuntimeError('Terminal operation identity conflict; retained receipt unchanged')
        if saved and saved['state'] != 'finished':
            raise RuntimeError('Terminal launch is unresolved; inspect retained receipt before reconciliation')
        paths = {name: directory / name for name in ('stdout', 'stderr')}
        if not saved:
            limits = read_limits(ROOT)
            saved = dict(operation=operation, signature=signature, state='launching',
                         started_at=time.time(), adapter_pid=os.getpid())
            save_receipt(receipt, saved)
            start, reason = time.monotonic(), 'exited'
            with paths['stdout'].open('wb') as out, paths['stderr'].open('wb') as err:
                child = subprocess.Popen(argv, cwd=Path.home(), stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=err, start_new_session=True,
                                         preexec_fn=lambda: apply_limits(limits))
                saved.update(state='running', pid=child.pid, pgid=child.pid)
                save_receipt(receipt, saved)
                try:
                    code = child.wait(timeout=limits.get('duration_seconds'))
                except subprocess.TimeoutExpired:
                    reason, code = 'timeout', 124
                finally:
                    # Keep the prior process-group cleanup policy. Even a cleanup
                    # failure retains the output and unresolved receipt for inspection.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                out.flush()
                err.flush()
                os.fsync(out.fileno())
                os.fsync(err.fileno())
            evidence = dict(executed=True, quiescent=True, host=socket.gethostname(),
                            exit_code=code, reason=reason,
                            duration_seconds=round(time.monotonic() - start, 3),
                            process_identity=dict(pid=child.pid, pgid=child.pid),
                            executor_identity=dict(system=os.uname().sysname,
                                                   machine=os.uname().machine,
                                                   executable=profile['executable'],
                                                   executable_sha256=profile['executable_sha256']))
            saved.update(state='finished', result=dict(exit_code=code, evidence=evidence))
            save_receipt(receipt, saved)
        result = dict(saved['result'], streams={})
        for name, path in paths.items():
            result[name], result['streams'][name] = store_stream(path, remote, 48 * 1024)
        # A lost upload/return can be resumed from the finished receipt without
        # launching the command again. No timer deletes unresolved or saved output.
        save_receipt(directory / 'published.json', result)
        return result


def recover_finished(remote, cfg):
    """Republish only a completed exact terminal receipt; never execute an argv."""
    import sqlite3
    from biomerte.worker import reconcile_receipt
    db = sqlite3.connect('file:' + str(ROOT / 'worker.sqlite') + '?mode=ro', uri=True)
    try:
        operations = [r[0] for r in db.execute("SELECT id FROM actions WHERE state='uncertain'")]
    finally:
        db.close()
    for operation in operations:
        directory = ROOT / 'terminal-results' / hashlib.sha256(operation.encode()).hexdigest()
        path = directory / 'receipt.json'
        if not path.exists():
            continue
        with (directory / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = json.loads(path.read_text())
            if saved['state'] != 'finished':
                continue
            action = remote.call('cap.status', {'operation': operation})
            expected = hashlib.sha256(json.dumps([operation, action['inputs'], cfg['terminal_profile']],
                                                 sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if saved['signature'] != expected or saved['operation'] != operation:
                raise RuntimeError('Terminal recovery identity mismatch')
            result = dict(saved['result'], streams={})
            for name in ('stdout', 'stderr'):
                result[name], result['streams'][name] = store_stream(directory / name, remote, 48 * 1024)
            reconcile_receipt(str(ROOT), remote, operation, 'succeeded', result)
            save_receipt(directory / 'published.json', result)


if __name__ == '__main__':
    os.umask(0o077)
    config = json.loads((ROOT / 'terminal-manifest.json').read_text())
    json.dump(execute(json.load(sys.stdin), config, Remote(config)), sys.stdout)
