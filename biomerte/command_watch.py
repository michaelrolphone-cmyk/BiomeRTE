"""Bounded output checkpoints and provider-owned process liveness (Python 3.4+)."""
import hashlib
import json
import os
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


def communicate(worker, proc, request, action, adapter):
    cfg = adapter.get('configuration', {})
    interval = cfg.get('heartbeat_interval_seconds', 60)
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
    last_signature, last = None, time.monotonic()
    next_check = 0
    stopping = False
    while reader.is_alive():
        now = time.monotonic()
        if now >= next_check:
            next_check = now + min(interval / 2, 5)
            try:
                state = worker.remote.call('cap.command.state', dict(provider=worker.remote.node,
                    operation=action['id'], generation=action['generation']))
                stopping = state['kill_requested'] or state['state'] == 'abandoned'
                if not stopping and os.path.exists(path):
                    with open(path) as source:
                        checkpoint = json.load(source)
                    if checkpoint['signature'] != last_signature:
                        worker.remote.call('agent.progress', dict(provider=worker.remote.node,
                            operation=action['id'], generation=action['generation'], checkpoint=checkpoint))
                        last_signature, last = checkpoint['signature'], now
            except Exception:
                # Network failure is not liveness evidence. The local wrapper
                # still owns custody and stops after the missed-liveness window.
                pass
        if stopping or now - last >= interval * cfg.get('missed_heartbeats_limit', 3):
            with open(path + '.stop', 'w') as out:
                out.write('stop')
            stopping = True
        reader.join(.2)
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
