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
    from biomerte.command_watch import OutputProgress, capture
    progress = OutputProgress(request.get('progress_path'))
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
                         started_at=time.time(), adapter_pid=os.getpid(), retention_version=1)
            save_receipt(receipt, saved)
            start, reason = time.monotonic(), 'exited'
            with paths['stdout'].open('wb') as out, paths['stderr'].open('wb') as err:
                child = subprocess.Popen(argv, cwd=Path.home(), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                         preexec_fn=lambda: apply_limits(limits))
                saved.update(state='running', pid=child.pid, pgid=child.pid)
                save_receipt(receipt, saved)
                counts = {}
                readers = [capture(child.stdout, paths['stdout'], counts, 'stdout'),
                           capture(child.stderr, paths['stderr'], counts, 'stderr')]
                try:
                    deadline = None if limits.get('duration_seconds') is None else start + limits['duration_seconds']
                    while child.poll() is None:
                        progress.report(paths)
                        if progress.stopped():
                            reason = 'stop_requested'
                            os.killpg(child.pid, signal.SIGTERM)
                            try:
                                child.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid, signal.SIGKILL)
                            break
                        if deadline is not None and time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(argv, limits['duration_seconds'])
                        time.sleep(.2)
                    code = child.wait()
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
                    for reader in readers:
                        reader.join()
                    progress.report(paths)
                out.flush()
                err.flush()
                os.fsync(out.fileno())
                os.fsync(err.fileno())
            evidence = dict(executed=True, quiescent=True, host=socket.gethostname(),
                            exit_code=code, reason=reason, output_retention=counts,
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
    failures = []
    for operation in operations:
        try:
            recover_one_finished(remote, cfg, operation, reconcile_receipt)
        except Exception as exc:
            failures.append({'operation': operation, 'error': type(exc).__name__})
    return failures


def recover_one_finished(remote, cfg, operation, reconcile_receipt):
    directory = ROOT / 'terminal-results' / hashlib.sha256(operation.encode()).hexdigest()
    path = directory / 'receipt.json'
    if not path.exists():
        return
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(path.read_text())
        if saved['state'] != 'finished':
            return
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


def expire_result(operation,historical=None):
    """Delete acknowledged generated diagnostics and identity, never a workspace."""
    import stat
    directory=ROOT/'terminal-results'/hashlib.sha256(operation.encode()).hexdigest()
    if not os.path.lexists(str(directory)):return {'expired':True}
    if directory.is_symlink() or os.path.realpath(str(directory))!=str(directory.absolute()) or directory.stat().st_uid!=os.getuid():raise RuntimeError('Unsafe expiry directory')
    known=('stdout','stderr','published.json','receipt.json','lock','expiry.json')
    for name in known:
        path=directory/name
        if os.path.lexists(str(path)):
            st=os.lstat(str(path))
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_uid!=os.getuid():raise RuntimeError('Unsafe expiry file')
    if not any(os.path.lexists(str(directory/name)) for name in known):
        # A prior completed expiry may have left unrelated user files only.
        return {'expired':True}
    lock=directory/'lock'
    fd=os.open(str(lock),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,384)
    with os.fdopen(fd,'a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for name in known:
            path=directory/name
            if os.path.lexists(str(path)):
                st=os.lstat(str(path))
                if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_uid!=os.getuid():raise RuntimeError('Expiry file changed before lock')
        receipt=directory/'receipt.json';pending=directory/'expiry.json'
        if pending.exists():
            value=json.loads(pending.read_text())
            if value.get('operation')!=operation or value.get('state')!='expiring':raise RuntimeError('Expiry identity differs')
        else:
            value=json.loads(receipt.read_text())
            if value.get('operation')!=operation:raise RuntimeError('Expiry identity differs')
            if value.get('state')!='acknowledged':
                if not isinstance(historical,dict) or value.get('state')!='finished' or historical.get('evidence',{}).get('quiescent') is not True or value.get('result',{}).get('evidence',{}).get('quiescent') is not True:raise RuntimeError('Unresolved terminal must remain')
                if any(historical.get(k)!=v for k,v in value['result'].items()):raise RuntimeError('Historical completion proof differs')
                for name in ('stdout','stderr'):
                    path=directory/name
                    if path.exists():
                        h=hashlib.sha256();size=0
                        with path.open('rb') as stream:
                            for block in iter(lambda:stream.read(65536),b''):h.update(block);size+=len(block)
                        expected=historical.get('streams',{}).get(name,{})
                        if expected.get('sha256')!=h.hexdigest() or expected.get('size')!=size:raise RuntimeError('Historical generated output changed')
            save_receipt(pending,{'operation':operation,'state':'expiring'})
        for name in ('stdout','stderr','published.json','receipt.json'):
            try:(directory/name).unlink()
            except FileNotFoundError:pass
        # Only the known generated identity files are removed. User-created
        # files keep their directory but do not retain an operation record.
        lock.unlink();pending.unlink()
        try:directory.rmdir()
        except OSError:
            if not any(directory.iterdir()):raise
    parent=os.open(str(directory.parent),os.O_RDONLY)
    try:os.fsync(parent)
    finally:os.close(parent)
    return {'expired':True}


def acknowledge_result(request):
    """Discard only new output copies after controller acknowledgement is durable."""
    operation = request['operation']
    if request.get('expire') is True:return expire_result(operation,request.get('historical_result'))
    directory = ROOT / 'terminal-results' / hashlib.sha256(operation.encode()).hexdigest()
    receipt = directory / 'receipt.json'
    if not receipt.exists():
        return {'acknowledged': True, 'legacy_preserved': True}
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(receipt.read_text())
        if saved.get('retention_version') != 1:
            return {'acknowledged': True, 'legacy_preserved': True}
        if saved.get('operation') != operation:
            raise RuntimeError('Terminal acknowledgement identity mismatch')
        if saved['state'] == 'acknowledged':
            if saved.get('result_digest') != request['result_digest']:
                raise RuntimeError('Terminal acknowledgement result mismatch')
        else:
            if saved['state'] != 'finished':
                raise RuntimeError('Unresolved terminal output cannot be retired')
            published = directory / 'published.json'
            value = json.loads(published.read_text())
            digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if digest != request['result_digest']:
                raise RuntimeError('Terminal acknowledgement result mismatch')
            summary = {k: saved[k] for k in ('operation', 'signature', 'retention_version')}
            summary.update(state='acknowledged', result_digest=digest)
            save_receipt(receipt, summary)
        # Identity remains durable before deleting redundant output. Retries are
        # idempotent even after partial deletion; no directory recursion occurs.
        for name in ('stdout', 'stderr', 'published.json'):
            path = directory / name
            if path.is_symlink():
                raise RuntimeError('Unsafe terminal diagnostic path')
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return {'acknowledged': True}

if __name__ == '__main__':
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == '--acknowledge':
        json.dump(acknowledge_result(json.load(sys.stdin)), sys.stdout)
    else:
        config = json.loads((ROOT / 'terminal-manifest.json').read_text())
        json.dump(execute(json.load(sys.stdin), config, Remote(config)), sys.stdout)
