"""macOS subprocess adapter for the shared BiomeRTE terminal contract."""
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


def execute(request, cfg, remote):
    profile = cfg['terminal_profile']
    limits = read_limits(ROOT)
    try:
        argv = check_argv(request['inputs']['argv'], profile)
    except ValueError as exc:
        return {'exit_code': 126, 'stdout': '', 'stderr': str(exc), 'streams': {},
                'evidence': {'executed': False, 'quiescent': True, 'reason': 'argv_rejected'}}

    start = time.monotonic()
    reason = 'exited'
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        paths = {name: Path(directory) / name for name in ('stdout', 'stderr')}
        with paths['stdout'].open('wb') as out, paths['stderr'].open('wb') as err:
            child = subprocess.Popen(argv, cwd=Path.home(), stdin=subprocess.DEVNULL, stdout=out,
                                     stderr=err, start_new_session=True, preexec_fn=lambda: apply_limits(limits))
            try:
                code = child.wait(timeout=limits.get('duration_seconds'))
            except subprocess.TimeoutExpired:
                reason, code = 'timeout', 124
            finally:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        evidence = dict(executed=True, quiescent=True, host=socket.gethostname(), exit_code=code,
                        reason=reason, duration_seconds=round(time.monotonic() - start, 3),
                        process_identity=dict(pid=child.pid, pgid=child.pid),
                        executor_identity=dict(system=os.uname().sysname, machine=os.uname().machine,
                                               executable=profile['executable'],
                                               executable_sha256=profile['executable_sha256']))
        result = dict(exit_code=code, streams={}, evidence=evidence)
        for name, path in paths.items():
            result[name], result['streams'][name] = store_stream(path, remote, 48 * 1024)
        return result


if __name__ == '__main__':
    config = json.loads((ROOT / 'terminal-manifest.json').read_text())
    json.dump(execute(json.load(sys.stdin), config, Remote(config)), sys.stdout)
