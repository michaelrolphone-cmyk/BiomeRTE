#!/usr/bin/env python3
"""Controller-independent Linux maintenance, including Python 3.4.

The transport reads kernel diagnostics without touching the execution ledger.
Only the executor process opens durable storage, and it commits command identity
before starting a process. A blocked executor never authorizes a RAM-only replay.
"""
from __future__ import print_function

import argparse
import binascii
import fcntl
import hashlib
import json
import multiprocessing
import os
import queue
import re
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

AGENT = 'linux-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as stream:
        return stream.read()


def private_file(path):
    stat = os.stat(path)
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('Credential and configuration files must be private to this user')
    return read_text(path)


def filesystem_type(path, mountinfo='/proc/self/mountinfo'):
    path = os.path.realpath(path)
    best = ('', None)
    for line in read_text(mountinfo).splitlines():
        before, after = line.split(' - ', 1)
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)),
                       before.split()[4])
        if path == mount or path.startswith(mount.rstrip('/') + '/'):
            if len(mount) > len(best[0]):
                best = (mount, after.split()[0])
    return best[1]


def require_durable_home(home):
    kind = filesystem_type(home)
    if kind is None or kind in ('tmpfs', 'ramfs', 'devtmpfs', 'proc', 'sysfs',
                                 'nfs', 'nfs4', 'cifs', 'smbfs', 'fuse.sshfs', '9p'):
        raise ValueError('Execution ledger requires a persistent local filesystem')
    return kind


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Maintenance redirects are forbidden',
                        headers, fp)


class Transport(object):
    def __init__(self, origin, key, host, cafile=None, timeout=10):
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('Expected an HTTPS origin without credentials or path')
        if len(key) != 64 or not re.match(r'^[0-9a-f]{64}$', key):
            raise ValueError('Invalid device credential')
        self.url = origin.rstrip('/') + '/maintenance/device'
        self.key, self.host, self.timeout = key, host, timeout
        # Python 3.4.2 urlopen() neither accepts context= nor verifies TLS by
        # default. An explicit HTTPSHandler works on that exact interpreter.
        context = ssl.create_default_context(cafile=cafile)
        self.opener = build_opener(ProxyHandler({}), NoRedirect(),
                                  HTTPSHandler(context=context, check_hostname=True))

    def exchange(self, body):
        request = Request(self.url, canonical(body), {
            'Authorization': 'Bearer ' + self.key,
            'Content-Type': 'application/json',
            'User-Agent': 'BiomeRTE-Independent-Linux-Maintenance/1'})
        # This is a network-operation timeout, never a command execution limit.
        with self.opener.open(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
        if not isinstance(result, dict):
            raise ValueError('Invalid maintenance response')
        return result

    def complete(self, identifier, result):
        ack = self.exchange({'operation': 'complete', 'id': identifier, 'result': result})
        if not (ack.get('accepted') is True and ack.get('id') == identifier
                and ack.get('host') == self.host
                and ack.get('result_sha256') == digest(result)):
            raise ValueError('Completion acknowledgement does not match durable result')
        return digest(result)


def collect_diagnostics(executor_pid=None):
    """Only procfs and network ioctls; never df, subprocesses, or controller APIs."""
    result = {'version': 1, 'agent': AGENT, 'pid': os.getpid()}
    for key, path in [('boot_id', '/proc/sys/kernel/random/boot_id'),
                      ('uptime', '/proc/uptime'), ('loadavg', '/proc/loadavg')]:
        try:
            result[key] = read_text(path).strip()
        except (OSError, IOError):
            pass
    try:
        wanted = {'MemTotal', 'MemFree', 'MemAvailable', 'Buffers', 'Cached',
                  'SwapTotal', 'SwapFree'}
        result['memory'] = dict((key, value.strip())
            for key, value in (line.split(':', 1)
                               for line in read_text('/proc/meminfo').splitlines())
            if key in wanted)
    except (OSError, IOError, ValueError):
        pass
    try:
        result['block_devices'] = [
            {'name': fields[2], 'counters': [int(x) for x in fields[3:]]}
            for fields in (line.split() for line in read_text('/proc/diskstats').splitlines())
            if fields[2].startswith(('mmcblk', 'sd', 'nvme'))]
    except (OSError, IOError, ValueError, IndexError):
        pass
    interfaces = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for line in read_text('/proc/net/dev').splitlines()[2:]:
                name, counters = line.rsplit(':', 1)
                name = name.strip()
                item = {'name': name, 'counters': [int(x) for x in counters.split()]}
                packed = struct.pack('256s', name.encode('ascii')[:15])
                try:
                    item['ipv4'] = socket.inet_ntoa(
                        fcntl.ioctl(sock.fileno(), 0x8915, packed)[20:24])
                except (OSError, IOError):
                    pass
                try:
                    raw = fcntl.ioctl(sock.fileno(), 0x8927, packed)[18:24]
                    item['mac'] = ':'.join('%02x' % value for value in bytearray(raw))
                except (OSError, IOError):
                    pass
                interfaces.append(item)
        result['interfaces'] = interfaces
        result['routes'] = read_text('/proc/net/route').strip()
    except (OSError, IOError, ValueError):
        pass
    if executor_pid:
        result['executor_pid'] = executor_pid
        try:
            result['executor_wait'] = read_text('/proc/%d/wchan' % executor_pid).strip()
        except (OSError, IOError):
            pass
    return result


class Executor(object):
    """Owns all SD-backed reads and writes; transport never calls these methods."""
    def __init__(self, home, events, starter=None):
        self.home, self.events = home, events
        self.lock = threading.RLock()
        self.failed = threading.Event()
        self.starter = starter or self.start_thread
        self.db = sqlite3.connect(os.path.join(home, 'ledger.sqlite'),
                                  check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS commands '
                        '(id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,'
                        'state TEXT NOT NULL,result TEXT)')
        uncertain = {'state': 'uncertain', 'reason':
                     'Maintenance executor restarted; reconcile recorded process and output '
                     'before requesting another execution.'}
        self.db.execute("UPDATE commands SET state='uncertain',result=? WHERE state='running'",
                        (canonical(uncertain).decode('ascii'),))
        self.db.commit()
        for identifier, result in self.db.execute(
                "SELECT id,result FROM commands WHERE state IN ('succeeded','failed','uncertain')"):
            self.events.put({'kind': 'result', 'id': identifier, 'result': json.loads(result)})

    def start_thread(self, job):
        threading.Thread(target=self.execute, args=(job,), daemon=True).start()

    def admit(self, job):
        identifier, argv = job.get('id'), job.get('argv')
        if (not isinstance(identifier, str) or not identifier or not isinstance(argv, list)
                or not argv or any(not isinstance(v, str) or '\0' in v for v in argv)
                or not argv[0].startswith('/')
                or not (job.get('cwd') is None or isinstance(job.get('cwd'), str))):
            raise ValueError('Invalid maintenance command')
        request_digest = digest({'argv': argv, 'cwd': job.get('cwd')})
        with self.lock:
            if self.failed.is_set():
                raise RuntimeError('Durable executor unavailable')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                prior = self.db.execute('SELECT request_digest FROM commands WHERE id=?',
                                        (identifier,)).fetchone()
                if prior:
                    if prior[0] != request_digest:
                        raise ValueError('Command identity reused')
                    self.db.rollback()
                    return False
                self.db.execute("INSERT INTO commands VALUES(?,?,'running',NULL)",
                                (identifier, request_digest))
                self.db.commit()  # No process or execution thread exists before this succeeds.
            except BaseException:
                self.db.rollback()
                raise
        self.starter(job)
        return True

    def execute(self, job):
        directory = os.path.join(self.home, 'commands',
                                 hashlib.sha256(job['id'].encode('utf-8')).hexdigest())
        process = None
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            stdout_path, stderr_path = (os.path.join(directory, name)
                                        for name in ('stdout', 'stderr'))
            with open(stdout_path, 'wb') as stdout, open(stderr_path, 'wb') as stderr:
                process = subprocess.Popen(job['argv'], cwd=job.get('cwd') or None,
                                           stdout=stdout, stderr=stderr,
                                           start_new_session=True)
                with open(os.path.join(directory, 'pid'), 'w') as stream:
                    stream.write(str(process.pid))
                code = process.wait()  # No software command lifetime or output truncation.
            result = {'state': 'succeeded' if code == 0 else 'failed',
                      'exit_code': code, 'stdout': read_text(stdout_path),
                      'stderr': read_text(stderr_path), 'output_directory': directory}
        except Exception as error:
            result = {'state': 'uncertain' if process is not None else 'failed',
                      'error': type(error).__name__, 'output_directory': directory}
            if process is not None:
                result['pid'] = process.pid
        try:
            with self.lock:
                self.db.execute('UPDATE commands SET state=?,result=? WHERE id=?',
                                (result['state'], canonical(result).decode('ascii'), job['id']))
                self.db.commit()
            self.events.put({'kind': 'result', 'id': job['id'], 'result': result})
        except Exception as error:
            self.failed.set()
            self.events.put({'kind': 'error', 'error': type(error).__name__})

    def acknowledge(self, items):
        with self.lock:
            for identifier, result_digest in items:
                row = self.db.execute('SELECT result FROM commands WHERE id=?',
                                      (identifier,)).fetchone()
                if not row or not row[0] or digest(json.loads(row[0])) != result_digest:
                    raise ValueError('Local completion acknowledgement mismatch')
                self.db.execute("UPDATE commands SET state='reported' WHERE id=?", (identifier,))
            self.db.commit()


def executor_main(home, incoming, outgoing):
    try:
        executor = Executor(home, outgoing)
        outgoing.put({'kind': 'ready'})
        while True:
            message = incoming.get()
            if executor.failed.is_set():
                raise RuntimeError('Durable executor unavailable')
            if message['kind'] == 'jobs':
                for job in message['jobs']:
                    executor.admit(job)
            elif message['kind'] == 'acks':
                executor.acknowledge(message['items'])
            else:
                raise ValueError('Unknown executor message')
            if executor.failed.is_set():
                raise RuntimeError('Durable executor unavailable')
            outgoing.put({'kind': 'ready'})
    except Exception as error:
        outgoing.put({'kind': 'error', 'error': type(error).__name__})


class Agent(object):
    def __init__(self, transport, incoming, outgoing, worker=None):
        self.transport, self.incoming, self.outgoing = transport, incoming, outgoing
        self.worker = worker
        self.ready, self.failed = False, False
        self.outbox, self.acks = {}, []
        self.error = None

    def drain(self):
        while True:
            try:
                message = self.outgoing.get_nowait()
            except queue.Empty:
                break
            if message['kind'] == 'ready' and not self.failed:
                self.ready = True
            elif message['kind'] == 'result':
                self.outbox[message['id']] = message['result']
            elif message['kind'] == 'error':
                self.failed, self.ready, self.error = True, False, message['error']
        if self.worker is not None and not self.worker.is_alive():
            self.failed, self.ready = True, False
            self.error = self.error or 'ExecutorExited'

    def send_executor(self, message):
        self.ready = False
        try:
            self.incoming.put_nowait(message)
        except queue.Full:
            self.failed, self.error = True, 'ExecutorQueueUnavailable'

    def step(self, diagnostics=None):
        self.drain()
        status = diagnostics if diagnostics is not None else collect_diagnostics(
            self.worker.pid if self.worker is not None else None)
        status['executor'] = ('unavailable' if self.failed else
                              'ready' if self.ready else 'waiting_for_durable_storage')
        if self.error:
            status['executor_error'] = self.error
        ack = self.transport.exchange({'operation': 'heartbeat', 'diagnostics': status})
        if ack.get('accepted') is not True or ack.get('host') != self.transport.host:
            raise ValueError('Heartbeat host acknowledgement mismatch')
        for identifier, result in list(self.outbox.items()):
            result_digest = self.transport.complete(identifier, result)
            self.acks.append((identifier, result_digest))
            del self.outbox[identifier]
        if not self.ready:
            return
        if self.acks:
            self.send_executor({'kind': 'acks', 'items': self.acks})
            self.acks = []
            return
        jobs = self.transport.exchange({'operation': 'poll'}).get('jobs')
        if not isinstance(jobs, list):
            raise ValueError('Invalid maintenance inbox')
        if jobs:
            self.send_executor({'kind': 'jobs', 'jobs': jobs})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('home')
    parser.add_argument('--runtime', default='/run/biomerte-maintenance')
    parser.add_argument('--probe', action='store_true',
                        help='Verify TLS, enrollment and read-only heartbeat; do not start executor')
    args = parser.parse_args(argv)
    os.umask(0o077)
    home = os.path.abspath(args.home)
    require_durable_home(home)
    config = json.loads(private_file(os.path.join(home, 'config.json')))
    key = private_file(os.path.join(home, 'device.key')).strip()
    transport = Transport(config['origin'], key, config['host'],
                          cafile=config.get('cafile'))
    if args.probe:
        result = transport.exchange({'operation': 'heartbeat',
                                     'diagnostics': dict(collect_diagnostics(),
                                                         executor='probe_only')})
        if result.get('accepted') is not True or result.get('host') != config['host']:
            raise ValueError('Heartbeat host acknowledgement mismatch')
        print(json.dumps(result, sort_keys=True))
        return
    # This lock and transport scratch are disposable; execution custody is not.
    os.makedirs(args.runtime, mode=0o700, exist_ok=True)
    lock = open(os.path.join(args.runtime, 'agent.lock'), 'a')
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    context = multiprocessing.get_context('fork')
    incoming, outgoing = context.Queue(maxsize=1), context.Queue()
    worker = context.Process(target=executor_main, args=(home, incoming, outgoing))
    worker.daemon = True
    worker.start()
    agent = Agent(transport, incoming, outgoing, worker)
    last_error = None
    while True:
        try:
            agent.step()
            last_error = None
        except Exception as error:
            category = type(error).__name__
            if category != last_error:
                print('maintenance transport retry: ' + category, flush=True)
                last_error = category
        time.sleep(1)


if __name__ == '__main__':
    main()
