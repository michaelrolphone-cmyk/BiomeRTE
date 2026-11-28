"""Independent privileged watchdog. No repeat, automatic resume or external listener."""
import ctypes
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from common import BACKEND, BACKEND_SHA256, atomic, digest, identity, read, settings
from hardware import Clock


def terminate(child):
    if child is None:
        return None
    if child.poll() is None:
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    return child.returncode


def watch(child, parent_alive, cancelled, duration, on_running=None):
    deadline = time.monotonic() + duration
    reported = False
    while child.poll() is None:
        if cancelled():
            return 'stopped'
        if not parent_alive():
            return 'interrupted'
        if time.monotonic() >= deadline:
            return 'completed'
        if on_running is not None and not reported:
            reported = on_running()
        time.sleep(.05)
    return 'completed' if child.returncode == 0 else 'failed'


def main(directory):
    os.umask(0o022)
    job = read(os.path.join(directory, 'request.json'))
    cfg = settings(job['settings'])
    path = os.path.join(directory, 'state.json')
    report = {'job_id': job['job_id'], 'state': 'starting', 'worker_identity': identity(os.getpid()), 'started_at': time.time(), 'settings': cfg}
    atomic(path, report)
    child = None
    hw = None
    lock = None
    cancelled = [False]
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: cancelled.__setitem__(0, True))
    try:
        fd = os.open('/run/lock/capabilities-fm-transmit.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if digest(BACKEND) != BACKEND_SHA256:
            raise RuntimeError('Installed FM backend changed; inspect and qualify before use')
        if identity(job['parent']['pid']) != job['parent']:
            raise RuntimeError('Controller no longer exists')
        hw = Clock()
        report['hardware_before'] = hw.acquire()
        expected_parent = os.getpid()
        def child_setup():
            # If the watchdog itself dies, ask the legacy backend to clean up.
            libc = ctypes.CDLL(None)
            if libc.prctl(1, signal.SIGINT, 0, 0, 0) != 0 or os.getppid() != expected_parent:
                os._exit(125)
        with open(os.path.join(directory, 'backend.log'), 'wb') as log:
            child = subprocess.Popen([BACKEND, '-f', '{:.6f}'.format(cfg['frequency_hz']/1e6), os.path.join(directory, 'audio.wav')], stdin=subprocess.DEVNULL, stdout=log, stderr=log, preexec_fn=child_setup)
            report['backend_identity'] = identity(child.pid)
            report['deadline_at'] = time.time() + cfg['duration_seconds']
            def running():
                state = hw.state()
                if state['clock_enabled']:
                    report.update(state='running', emission_clock_observed_at=time.time(), hardware_during=state)
                    atomic(path, report)
                    return True
                return False
            report['state'] = watch(child, lambda: identity(job['parent']['pid']) == job['parent'], lambda: cancelled[0] or os.path.exists(os.path.join(directory, 'stop')), cfg['duration_seconds'], running)
            if 'emission_clock_observed_at' not in report and report['state'] == 'completed':
                raise RuntimeError('Backend finished without an observed enabled clock')
    except Exception as error:
        report.update(state='failed', error=str(error)[:1000])
    finally:
        try:
            report['backend_exit_code'] = terminate(child)
            report['cleanup'] = hw.cleanup() if hw is not None else {'performed': False}
            if report['cleanup'].get('performed') and not report['cleanup'].get('verified'):
                report.update(state='failed', error='Clock cleanup not verified')
        except Exception as error:
            report.update(state='failed', error='Cleanup failed: '+str(error)[:500], cleanup={'verified': False})
        finally:
            if hw is not None:
                hw.close()
            if lock is not None:
                lock.close()
        report['finished_at'] = time.time()
        report['emission_verified_by_sdr'] = False
        atomic(path, report)
        try:
            os.unlink(os.path.join(directory, 'audio.wav'))
        except OSError:
            pass


if __name__ == '__main__':
    main(sys.argv[1])
