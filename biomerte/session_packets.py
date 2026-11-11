"""Bounded temporary transport for signed coordination packets, not a journal."""
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from .store import Fault, encode

CHUNK_BYTES = 49152
MAX_BYTES = 64 * 1024 * 1024
MAX_TRANSFERS = 4
TRANSFER_SECONDS = 3600


def _private_directory(path):
    if not os.path.exists(path):
        os.makedirs(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or os.path.realpath(path) != path:
        raise Fault('Unsafe packet directory', 409)


def _open(path, flags):
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise Fault('Unsafe packet file', 409)
    return fd


class PacketReceiver:
    def __init__(self, directory, node, site, process):
        self.directory, self.node, self.site, self.process = os.path.abspath(directory), node, site, process
        _private_directory(self.directory)

    def _discard(self, path):
        if set(os.listdir(path)) - {'payload', 'meta.json', 'lock', 'meta.next'}:
            raise Fault('Unknown file in private transfer directory', 409)
        for name in os.listdir(path):
            file_path = os.path.join(path, name)
            info = os.lstat(file_path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Fault('Unsafe transfer cleanup target', 409)
        for name in os.listdir(path):
            os.unlink(os.path.join(path, name))
        os.rmdir(path)

    @contextlib.contextmanager
    def _locked(self):
        lock = _open(os.path.join(self.directory, '.lock'), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock)

    def prune(self):
        with self._locked():
            return self._prune_locked()

    def _prune_locked(self):
        removed = 0
        for name in os.listdir(self.directory):
            if not re.match(r'^[0-9a-f]{64}$', name):
                continue
            path = os.path.join(self.directory, name)
            _private_directory(path)
            lock = _open(os.path.join(path, 'lock'), os.O_CREAT | os.O_RDWR)
            try:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                created = os.path.getmtime(path)
                meta_path = os.path.join(path, 'meta.json')
                if os.path.exists(meta_path):
                    with os.fdopen(_open(meta_path, os.O_RDONLY), 'r') as handle:
                        created = json.load(handle)['created']
                if created < time.time() - TRANSFER_SECONDS:
                    self._discard(path)
                    removed += 1
            finally:
                os.close(lock)
        return removed

    def accept(self, args):
        with self._locked():
            return self._accept_locked(args)

    def _accept_locked(self, args):
        transfer, sender = args.get('transfer'), args.get('from')
        total, size, index = args.get('total'), args.get('bytes'), args.get('index')
        if (not isinstance(transfer, str) or not re.match(r'^[0-9a-f]{64}$', transfer) or
                not isinstance(sender, str) or not re.match(r'^[0-9a-f]{64}$', sender) or
                args.get('to') != self.node or not isinstance(args.get('session'), str) or
                not re.match(r'^[A-Za-z0-9_.:-]{1,128}$', args['session']) or
                type(size) is not int or not 0 < size <= MAX_BYTES or
                type(total) is not int or total != int(math.ceil(size / float(CHUNK_BYTES))) or
                type(index) is not int or not 0 <= index < total):
            raise Fault('Invalid packet transfer identity', 400)
        chunk = args.get('chunk')
        if not isinstance(chunk, str) or len(chunk) > 65536:
            raise Fault('Invalid packet chunk', 400)
        try:
            raw = base64.b64decode(chunk.encode('ascii'), validate=True)
        except (ValueError, UnicodeError):
            raise Fault('Invalid packet encoding', 400)
        expected = min(CHUNK_BYTES, size - index * CHUNK_BYTES)
        if len(raw) != expected or base64.b64encode(raw).decode('ascii') != chunk:
            raise Fault('Packet chunk length mismatch', 400)
        self._prune_locked()
        path = os.path.join(self.directory, transfer)
        if not os.path.exists(path):
            if index != 0:
                return {'accepted': True, 'transfer': transfer, 'index': index,
                        'complete': False, 'next_index': 0}
            if len([n for n in os.listdir(self.directory) if re.match(r'^[0-9a-f]{64}$', n)]) >= MAX_TRANSFERS:
                raise Fault('Local transfer capacity is full', 429)
            os.mkdir(path, 0o700)
        _private_directory(path)
        lock = _open(os.path.join(path, 'lock'), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            meta_path = os.path.join(path, 'meta.json')
            ident = {'from': sender, 'to': self.node, 'session': args['session'],
                     'transfer': transfer, 'total': total, 'bytes': size}
            if os.path.exists(meta_path):
                with os.fdopen(_open(meta_path, os.O_RDONLY), 'r') as handle:
                    meta = json.load(handle)
                if any(meta.get(k) != v for k, v in ident.items()):
                    raise Fault('Transfer identity changed', 409)
            else:
                meta = dict(ident, next_index=0, created=time.time())
            payload = os.path.join(path, 'payload')
            fd = _open(payload, os.O_CREAT | os.O_RDWR)
            try:
                if index < meta['next_index']:
                    os.lseek(fd, index * CHUNK_BYTES, os.SEEK_SET)
                    if os.read(fd, len(raw)) != raw:
                        raise Fault('Repeated packet chunk changed', 409)
                elif index == meta['next_index']:
                    # Discard only an uncommitted partial tail left by a crash.
                    os.ftruncate(fd, index * CHUNK_BYTES)
                    os.lseek(fd, index * CHUNK_BYTES, os.SEEK_SET)
                    view = memoryview(raw)
                    while view:
                        count = os.write(fd, view)
                        view = view[count:]
                    os.fsync(fd)
                    meta['next_index'] += 1
                else:
                    return {'accepted': True, 'transfer': transfer, 'index': index,
                            'complete': False, 'next_index': meta['next_index']}
            finally:
                os.close(fd)
            staged = os.path.join(path, 'meta.next')
            with os.fdopen(_open(staged, os.O_CREAT | os.O_WRONLY | os.O_TRUNC), 'w') as handle:
                handle.write(encode(meta)); handle.flush(); os.fsync(handle.fileno())
            os.replace(staged, meta_path)
            if meta['next_index'] < total:
                return {'accepted': True, 'transfer': transfer, 'index': index,
                        'complete': False, 'next_index': meta['next_index']}
            with os.fdopen(_open(payload, os.O_RDONLY), 'rb') as handle:
                data = handle.read(MAX_BYTES + 1)
            if len(data) != size or hashlib.sha256(data).hexdigest() != transfer:
                self._discard(path)
                raise Fault('Complete packet digest mismatch', 400)
            try:
                packet = json.loads(data.decode('utf-8'))
            except (ValueError, UnicodeError):
                self._discard(path)
                raise Fault('Invalid complete packet JSON', 400)
            if (not isinstance(packet, dict) or not isinstance(packet.get('payload'), dict) or
                    not isinstance(packet['payload'].get('data'), dict)):
                self._discard(path)
                raise Fault('Invalid complete packet envelope', 400)
            body = packet['payload']
            session = body['data'].get('session', body['data'].get('id'))
            if (body.get('format') != 1 or body.get('kind') not in ('session-invitation', 'session-state', 'session-acceptance') or
                    body.get('sender') != sender or body.get('to') != self.node or
                    body.get('site') != self.site or session != args['session']):
                self._discard(path)
                raise Fault('Signed packet transport binding mismatch', 403)
            try:
                # The domain receiver verifies the full signature against its
                # locally trusted key before joining or importing any state.
                receipt = self.process(packet)
            except Fault as error:
                if error.status in (400, 403, 409, 410):
                    self._discard(path)
                raise
            self._discard(path)
            return {'accepted': True, 'transfer': transfer, 'index': index,
                    'complete': True, 'receipt': receipt}
        finally:
            os.close(lock)


def deliver_packet(send, recipient, packet):
    data = encode(packet).encode('utf-8')
    if not 0 < len(data) <= MAX_BYTES:
        raise Fault('Signed packet exceeds transfer limit', 413)
    body = packet['payload']
    session = body['data'].get('session', body['data'].get('id'))
    if body['to'] != recipient:
        raise Fault('Packet recipient differs from requested destination', 403)
    transfer = hashlib.sha256(data).hexdigest()
    total = int(math.ceil(len(data) / float(CHUNK_BYTES)))
    index, calls = 0, 0
    while index < total and calls < total * 3 + 3:
        chunk = base64.b64encode(data[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]).decode('ascii')
        result = send({'session': session, 'to': recipient, 'transfer': transfer,
                       'index': index, 'total': total, 'bytes': len(data), 'chunk': chunk})
        calls += 1
        if (result.get('accepted') is not True or result.get('transfer') != transfer or
                result.get('index') != index):
            raise Fault('Packet receipt differs from request', 503)
        if result.get('complete') is True:
            return result['receipt']
        next_index = result.get('next_index', index + 1)
        if type(next_index) is not int or not 0 <= next_index < total:
            raise Fault('Invalid packet continuation', 503)
        index = next_index
    raise Fault('Packet delivery did not complete within its retry bound', 503)
