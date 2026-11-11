"""Bounded output checkpoints and provider-owned process liveness (Python 3.4+)."""
import hashlib
import json
import os
import signal
import threading
import time


class OutputProgress:
    def __init__(self, path):
        self.path = path
        self.signature = None

    def stopped(self):
        return bool(self.path and os.path.exists(self.path + '.stop'))

    def report(self, paths):
        if not self.path:
            return
        tails = {}
        for name, path in paths.items():
            try:
                with open(str(path), 'rb') as source:
                    source.seek(0, 2)
                    size = source.tell()
                    source.seek(max(0, size - 4096))
                    tails[name] = source.read().decode('utf-8', 'replace')
            except OSError:
                tails[name] = ''
        signature = hashlib.sha256(json.dumps(tails, sort_keys=True).encode()).hexdigest()
        if signature == self.signature or not any(tails.values()):
            return
        self.signature = signature
        temporary = self.path + '.tmp'
        with open(temporary, 'w') as out:
            json.dump({'output': tails, 'signature': signature}, out)
        os.replace(temporary, self.path)


class OutputLiveness:
    def __init__(self, windows=3):
        self.windows = windows
        self.signature = None
        self.unchanged = 0
        self.started = False
        self.stalled = False

    def observe(self, progress):
        signature = progress.get('signature')
        advanced = signature is not None and signature != self.signature
        self.unchanged = 0 if advanced or not self.started else self.unchanged + 1
        self.started = True
        self.signature = signature
        stalled = self.unchanged >= self.windows
        changed = advanced or stalled != self.stalled
        self.stalled = stalled
        return changed, dict(progress, stalled_output=stalled,
                             unchanged_output_windows=self.unchanged)


def communicate(worker, proc, request, action, adapter):
    cfg = adapter.get('configuration', {})
    interval = min(cfg.get('heartbeat_interval_seconds', 60) / 2, 5)
    path = request['progress_path']
    result, failure = [], []
    def read():
        try:
            result.append(proc.communicate(json.dumps(request).encode()))
        except BaseException as exc:
            failure.append(exc)
    reader = threading.Thread(target=read)
    reader.daemon = True
    reader.start()
    liveness = OutputLiveness()
    next_check, stop_started, term_sent, kill_sent = 0, None, False, False
    args = dict(provider=worker.remote.node, operation=action['id'], generation=action['generation'])
    while reader.is_alive():
        now = time.monotonic()
        if now >= next_check:
            next_check = now + interval
            try:
                state = worker.remote.call('cap.command.state', args)
                if state['kill_requested'] or state['state'] == 'abandoned':
                    if stop_started is None:
                        stop_started = now
                        with open(path + '.stop', 'w') as out:
                            out.write('stop')
                elif stop_started is None:
                    worker.remote.call('cap.heartbeat', args)
                    checkpoint = {}
                    if os.path.exists(path):
                        with open(path) as source:
                            checkpoint = json.load(source)
                    changed, checkpoint = liveness.observe(checkpoint)
                    if changed:
                        worker.remote.call('agent.progress', dict(args, checkpoint=checkpoint))
            except Exception:
                # Transport loss alone does not terminate an operational process.
                pass
        if stop_started is not None:
            age = now - stop_started
            sig = signal.SIGKILL if age >= 4 and not kill_sent else signal.SIGTERM if age >= 2 and not term_sent else None
            if sig is not None:
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass
                term_sent = True
                kill_sent = kill_sent or sig == signal.SIGKILL
        reader.join(min(.2, interval))
    if failure:
        raise failure[0]
    return result[0]


def capture(pipe, path, counts, name, maximum=1048576):
    """Read continuously into a bounded disk tail; never block the child on RPC."""
    tail = bytearray()
    def drain():
        count = 0
        with open(str(path), 'w+b') as output:
            while True:
                block = os.read(pipe.fileno(), 65536)
                if not block:
                    break
                count += len(block)
                tail.extend(block)
                if len(tail) > maximum:
                    del tail[:-maximum]
                output.seek(0)
                output.write(tail)
                output.truncate()
                output.flush()
            counts[name] = {'bytes_seen': count, 'retained_bytes': len(tail), 'truncated': count > len(tail)}
        pipe.close()
    thread = threading.Thread(target=drain)
    thread.daemon = True
    thread.start()
    return thread
