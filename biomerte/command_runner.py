"""Detached, durable adapter custody. Python 3.4+, no network dependency."""
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time


def save(path, value):
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read(path):
    with open(path) as source:
        return json.load(source)


def location(root, operation):
    return os.path.join(root, hashlib.sha256(operation.encode()).hexdigest())


def locked(path):
    handle = open(path, 'a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def inspect(directory):
    result_path = os.path.join(directory, 'completion.json')
    if os.path.exists(result_path):
        return read(result_path)
    marker = os.path.join(directory, 'state.json')
    if not os.path.exists(marker):
        return {'state': 'starting'}
    state = read(marker)
    handle = locked(os.path.join(directory, 'custody.lock'))
    if handle is None:
        return state
    handle.close()
    # A released custody lock with no completion cannot prove the effect.
    return {'state': 'interrupted', 'reason': 'runner lost custody; effect requires reconciliation',
            'last_observation': state, 'replay_allowed': False}


def ensure(root, operation, signature, argv, request, timeout=None):
    directory = location(root, operation)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    spec = dict(operation=operation, signature=signature, argv=argv, request=request,
                timeout=timeout, cwd=os.getcwd(), environment=dict(os.environ))
    # A separate preparation lock does not interfere with the lifetime custody lock.
    with open(os.path.join(directory, 'prepare.lock'), 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = os.path.join(directory, 'request.json')
        if os.path.exists(path):
            prior = read(path)
            if any(prior[k] != spec[k] for k in ('operation', 'signature', 'argv', 'request')):
                raise ValueError('Runner operation identity conflict')
        else:
            save(path, spec)
        if not os.path.exists(os.path.join(directory, 'state.json')):
            # Several launch attempts are harmless: custody.lock and the durable
            # marker below permit exactly one adapter launch, including after restart.
            with open(os.path.join(directory, 'runner.log'), 'ab') as log:
                subprocess.Popen([sys.executable, '-S', os.path.abspath(__file__), directory],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
    return inspect(directory)


def run(directory):
    handle = locked(os.path.join(directory, 'custody.lock'))
    if handle is None:
        return
    with handle:
        marker = os.path.join(directory, 'state.json')
        if os.path.exists(marker):
            return  # Never replay a launch, even if its previous runner died.
        spec = read(os.path.join(directory, 'request.json'))
        state = dict(state='launching', operation=spec['operation'],
                     signature=spec['signature'], runner_pid=os.getpid(), observed_at=time.time())
        save(marker, state)  # Must be durable BEFORE the adapter can have effects.
        process = None
        started = time.monotonic()
        try:
            with open(os.path.join(directory, 'input.json'), 'wb') as inp:
                inp.write(json.dumps(spec['request']).encode())
            with open(os.path.join(directory, 'input.json'), 'rb') as inp, \
                    open(os.path.join(directory, 'stdout'), 'wb') as out, \
                    open(os.path.join(directory, 'stderr'), 'wb') as err:
                process = subprocess.Popen(spec['argv'], stdin=inp, stdout=out, stderr=err,
                                           cwd=spec['cwd'], env=spec['environment'], close_fds=True)
                state.update(state='running', adapter_pid=process.pid)
                while process.poll() is None:
                    state['observed_at'] = time.time()
                    save(marker, state)
                    if spec['timeout'] is not None and time.monotonic() - started >= spec['timeout']:
                        process.kill()
                        process.wait()
                        save(os.path.join(directory, 'completion.json'),
                             dict(state='uncertain', reason='Adapter timeout; inspect effects',
                                  returncode=process.returncode))
                        return
                    time.sleep(.2)
                for stream in (out, err):
                    stream.flush()
                    os.fsync(stream.fileno())
            save(os.path.join(directory, 'completion.json'),
                 dict(state='finished', returncode=process.returncode, observed_at=time.time()))
        except BaseException as exc:
            # No claim of quiescence: adapter descendants may still own effects.
            save(os.path.join(directory, 'completion.json'),
                 dict(state='uncertain', reason=str(exc), adapter_pid=process.pid if process else None))


if __name__ == '__main__':
    os.umask(0o077)
    run(sys.argv[1])
