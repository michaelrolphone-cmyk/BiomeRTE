"""Reserve local sequence ranges; never persist a stream payload."""
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time


class StreamSequences:
    def __init__(self, path=None, block_size=4096):
        if type(block_size) is not int or block_size < 1:
            raise ValueError('Invalid sequence reservation size')
        self.path = Path(path) if path is not None else None
        self.block_size = block_size
        self.lock = threading.Lock()
        self.ranges = {}

    @staticmethod
    def binding(grant):
        return json.dumps([grant[k] for k in
                           ('authority', 'id', 'epoch', 'generation', 'lane')],
                          separators=(',', ':'))

    def reserve(self, binding, expires_at, previous):
        if self.path is None:
            return previous + 1, previous + self.block_size
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        db = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS stream_sequences '
                       '(binding TEXT PRIMARY KEY, highwater INTEGER NOT NULL, expires_at REAL NOT NULL)')
            db.execute('DELETE FROM stream_sequences WHERE expires_at<=?', (time.time(),))
            row = db.execute('SELECT highwater FROM stream_sequences WHERE binding=?',
                             (binding,)).fetchone()
            previous = max(previous, row[0] if row else 0)
            end = previous + self.block_size
            db.execute('INSERT OR REPLACE INTO stream_sequences VALUES(?,?,?)',
                       (binding, end, expires_at))
            # Commit custody before returning any number in this range.
            db.commit()
            return previous + 1, end
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def next(self, grant):
        expires_at = grant['expires_at']
        now = time.time()
        if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                or not math.isfinite(expires_at) or expires_at <= now):
            raise ValueError('Expired stream sequence binding')
        binding = self.binding(grant)
        with self.lock:
            current = self.ranges.get(binding)
            if current is None or current[0] >= current[1]:
                for old, value in list(self.ranges.items()):
                    if value[2] <= now:
                        self.ranges.pop(old)
                start, end = self.reserve(binding, expires_at, current[1] if current else 0)
                current = [start - 1, end, expires_at]
                self.ranges[binding] = current
            current[0] += 1
            return current[0]
