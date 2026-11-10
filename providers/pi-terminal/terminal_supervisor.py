"""Supervise Biome terminal jobs using the installed Pi journal and process identity helpers."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

from sdk import Journal, monotonic
from supervisor import pathlib, process_group_members, process_identity
from terminal_limits import apply_limits


def execute(journal_path, job_id, progress_path=None):
    from biomerte.command_watch import OutputProgress, capture
    progress = OutputProgress(progress_path)
    j = Journal(journal_path)
    row = j.db.execute('SELECT body,state FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row or row[1] != 'queued':
        return
    body = json.loads(row[0])
    argv = body['argv']
    limits = body['limits']
    work = pathlib.Path(body['workspace']).resolve()
    work.mkdir(parents=True, exist_ok=True, mode=0o700)

    def restrict():
        if sys.platform == 'linux':
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
                raise RuntimeError('Cannot bind child custody')
            if os.getppid() == 1:
                os._exit(125)
        apply_limits(limits)
        os.umask(0o077)
    with j.db:
        changed = j.db.execute("UPDATE jobs SET state='launching',pid=? WHERE id=? AND state='queued'", (os.getpid(), job_id)).rowcount
    if not changed:
        return
    j.put('supervisor:' + job_id, process_identity(os.getpid()))
    start = monotonic()
    out = work / 'stdout.log'
    err = work / 'stderr.log'
    reason = None
    process = None
    try:
        with out.open('wb') as stdout, err.open('wb') as stderr:
            process = subprocess.Popen(argv, cwd=str(work), env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(work)}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, preexec_fn=restrict)
            counts = {}
            readers = [capture(process.stdout, out, counts, 'stdout'), capture(process.stderr, err, counts, 'stderr')]
            j.put('process:' + job_id, {'pid': process.pid, 'kernel_identity': process_identity(process.pid), 'supervisor_pid': os.getpid(), 'started_monotonic': start, 'argv_sha256': hashlib.sha256(json.dumps(argv).encode()).hexdigest()})
            with j.db:
                j.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
            while process.poll() is None:
                progress.report({'stdout': out, 'stderr': err})
                state, lease = j.db.execute('SELECT state,lease FROM jobs WHERE id=?', (job_id,)).fetchone()
                if progress.stopped() or state == 'stop_requested' or (lease is not None and monotonic() >= lease):
                    reason = 'stop_requested' if progress.stopped() or state == 'stop_requested' else 'lease_or_duration_expired'
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    break
                time.sleep(0.2)
            if process_group_members(process.pid):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    if not process_group_members(process.pid):
                        break
                    time.sleep(0.02)
            for reader in readers:
                reader.join()
            progress.report({'stdout': out, 'stderr': err})
            result = {'output_retention': counts, 'exit_code': process.returncode, 'duration_seconds': monotonic() - start, 'reason': reason, 'stdout': str(out), 'stderr': str(err), 'process_identity': j.get('process:' + job_id), 'quiescent': not process_group_members(process.pid)}
    except BaseException as exc:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        result = {'reason': 'supervisor_error', 'error': str(exc), 'quiescent': True, 'process_identity': {'supervisor_pid': os.getpid()}, 'exit_code': -1}
    with j.db:
        j.db.execute("UPDATE jobs SET state='finished',result=? WHERE id=?", (json.dumps(result), job_id))
if __name__ == '__main__':
    execute(sys.argv[1], sys.argv[2])
