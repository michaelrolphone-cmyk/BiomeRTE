"""Opt-in bounded resident diagnostics; never records request values (Python 3.4+)."""
import argparse
import contextlib
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time

class PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode)

class RequestTrace:
    def __init__(self, path, slow_seconds=1):
        self.slow_seconds = slow_seconds
        self.active = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.records = queue.Queue(maxsize=64)
        self.logger = logging.Logger('biomerte.request_trace')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.handler = PrivateRotatingHandler(path, maxBytes=1048576, backupCount=1)
        self.logger.addHandler(self.handler)

    def emit(self, value):
        try:
            self.records.put_nowait(dict(value, observed_at=time.time()))
        except queue.Full:
            pass

    def drain(self):
        for unused in range(64):
            try:
                value = self.records.get_nowait()
            except queue.Empty:
                return
            try:
                self.logger.warning(json.dumps(value, sort_keys=True))
            except Exception:
                pass

    @contextlib.contextmanager
    def operation(self, label):
        key = threading.get_ident()
        value = {'operation': label, 'phase': 'application', 'started': time.monotonic()}
        with self.lock:
            previous = self.active.get(key)
            self.active[key] = value
        try:
            yield
        finally:
            elapsed = time.monotonic() - value['started']
            if elapsed >= self.slow_seconds:
                self.emit({'event':'completed_slow', 'operation':label,
                           'seconds':round(elapsed, 4), 'thread':key})
            with self.lock:
                if previous is None:
                    self.active.pop(key, None)
                else:
                    self.active[key] = previous

    @contextlib.contextmanager
    def phase(self, label):
        key = threading.get_ident()
        with self.lock:
            value = self.active.get(key)
            previous = value.get('phase') if value else None
            if value:
                value['phase'] = label
        try:
            yield
        finally:
            if value:
                with self.lock:
                    value['phase'] = previous

    def sample(self):
        now = time.monotonic()
        with self.lock:
            pending = {key:dict(value) for key,value in self.active.items()
                       if now-value['started'] >= self.slow_seconds}
        if not pending:
            return
        current = sys._current_frames()
        for key,value in pending.items():
            frame = current.get(key)
            stack = []
            while frame is not None and len(stack) < 24:
                stack.append({'file':frame.f_code.co_filename,
                              'line':frame.f_lineno, 'function':frame.f_code.co_name})
                frame = frame.f_back
            self.emit({'event':'pending_slow', 'thread':key,
                       'operation':value['operation'], 'phase':value['phase'],
                       'seconds':round(now-value['started'], 4), 'stack':stack})

    def watch(self):
        while not self.stop.wait(1):
            try:
                self.sample()
                self.drain()
            except Exception:
                pass

    def close(self):
        self.stop.set()
        self.drain()
        self.handler.close()

def install(trace, application, store, operations):
    call, tick = application.call, application.tick
    execute, connect = store._execute, store.connect

    def traced_call(self, name, *args, **kwargs):
        label = name if name in operations else 'unknown_operation'
        with trace.operation(label):
            return call(self, name, *args, **kwargs)

    def traced_tick(self, realm):
        with trace.operation('tick.' + ('site' if realm == 'site' else 'local')):
            return tick(self, realm)

    def traced_connect(self):
        with trace.phase('sqlite.connect'):
            return connect(self)

    def traced_execute(self, fn, cached=False, read_only=False):
        def invoke(tx):
            with trace.phase('operation'):
                result = fn(tx)
            with trace.lock:
                value = trace.active.get(threading.get_ident())
                if value:
                    value['phase'] = 'sqlite.commit_close'
            return result
        with trace.phase('sqlite.transaction'):
            return execute(self, invoke, cached, read_only)

    application.call, application.tick = traced_call, traced_tick
    store._execute, store.connect = traced_execute, traced_connect

    def restore():
        application.call, application.tick = call, tick
        store._execute, store.connect = execute, connect
    return restore

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--diagnostic-log', required=True)
    args, remaining = parser.parse_known_args(argv)
    from biomerte.app import Application
    from biomerte.api import OPERATIONS
    from biomerte.store import Store
    from biomerte.__main__ import main as serve
    trace = RequestTrace(args.diagnostic_log)
    restore = install(trace, Application, Store, OPERATIONS)
    watcher = threading.Thread(target=trace.watch)
    watcher.daemon = True
    watcher.start()
    try:
        return serve(remaining)
    finally:
        trace.stop.set()
        watcher.join(2)
        restore()
        trace.close()

if __name__ == '__main__':
    sys.exit(main())
