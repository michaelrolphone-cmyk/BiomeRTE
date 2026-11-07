#!/usr/bin/env python3
"""Prepared, mirror-only recovery helper. Fill reviewed inputs before use.

Compatible with Python 3.4. It never constructs Application, writes the remote
journal, edits service units, or replaces node identity or the local realm.
"""
import errno
import fcntl
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

CONTROLLER = 'biomerte.service'
GATEWAY = 'biomerte-gateway.service'


class Refused(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, message):
    if not condition:
        raise Refused(message)


def file_digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            value.update(chunk)
    return value.hexdigest()


def process_argv(path):
    with open(path, 'rb') as stream:
        return stream.read().decode('utf-8').rstrip('\0').split('\0')


def regular(path):
    value = os.lstat(path)
    require(stat.S_ISREG(value.st_mode) and value.st_nlink == 1,
            'Expected a regular, unshared file')
    return value


def read_db(path):
    regular(path)
    return sqlite3.connect('file:' + quote(path, safe='/') + '?mode=ro',
                           uri=True, timeout=2, isolation_level=None)


def position(path):
    db = read_db(path)
    try:
        rows = db.execute('SELECT id,seq,hash FROM position').fetchall()
        require(len(rows) == 1 and rows[0][0] == 1, 'Invalid mirror position table')
        seq, value = rows[0][1:]
        require(type(seq) is int and seq >= 0, 'Invalid mirror sequence')
        require(value == '' if seq == 0 else bool(re.match(r'^[0-9a-f]{64}$', value)),
                'Invalid mirror anchor')
        return seq, value
    finally:
        db.close()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_manifest(manifest, ca, needs_tls=True):
    for name in ('site', 'gateway_origin', 'source_node', 'hash', 'snapshot_sha256'):
        require(isinstance(manifest.get(name), str) and manifest[name],
                'Unfilled or invalid manifest')
    require(re.match(r'^[A-Za-z0-9_.:-]{1,128}$', manifest['site']), 'Invalid site')
    for name in ('source_node', 'hash', 'snapshot_sha256'):
        require(re.match(r'^[0-9a-f]{64}$', manifest[name]), 'Invalid manifest digest')
    for name in ('sequence', 'snapshot_bytes'):
        require(type(manifest.get(name)) is int and manifest[name] > 0,
                'Invalid manifest size or sequence')
    origin = urlsplit(manifest['gateway_origin'])
    require(origin.scheme == 'https' and origin.hostname and not origin.username and
            not origin.password and not origin.query and not origin.fragment and
            origin.path in ('', '/'), 'Invalid gateway origin')
    if needs_tls:
        require('-----BEGIN CERTIFICATE-----' in ca, 'Reviewed TLS CA is required')


# Run as root only to inspect ownership. No process is signalled or attached.
# Fail closed if any extant process cannot be inspected.
OWNER_SCAN = r'''
import errno,json,os,stat,sys
paths=set(os.path.abspath(p) for p in sys.argv[1:])
identities=set()
for p in paths:
    try:
        s=os.stat(p);identities.add((s.st_dev,s.st_ino))
    except OSError as e:
        if e.errno!=errno.ENOENT:raise
owners=set();unreadable=set()
for pid in os.listdir('/proc'):
    if not pid.isdigit():continue
    base='/proc/'+pid
    try:
        for fd in os.listdir(base+'/fd'):
            f=base+'/fd/'+fd
            try:
                link=os.readlink(f).replace(' (deleted)','')
                s=os.stat(f)
                if link in paths or (s.st_dev,s.st_ino) in identities:owners.add(int(pid))
            except OSError as e:
                if e.errno not in (errno.ENOENT,errno.ESRCH):raise
        with open(base+'/maps') as stream:
            for line in stream:
                parts=line.rstrip().split(None,5)
                device=parts[3].split(':')
                mapped=(os.makedev(int(device[0],16),int(device[1],16)),int(parts[4]))
                if mapped in identities or (len(parts)==6 and parts[5].replace(' (deleted)','') in paths):
                    owners.add(int(pid))
    except OSError as e:
        if e.errno not in (errno.ENOENT,errno.ESRCH):unreadable.add(int(pid))
print(json.dumps({'owners':sorted(owners),'unreadable':sorted(unreadable)}))
'''

HASH_SYSTEM_FILES = r'''
import hashlib,json,os,stat,sys
result={}
for path in sys.argv[1:]:
    s=os.lstat(path)
    if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:raise ValueError('Unexpected unit file')
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(65536),b''):digest.update(chunk)
    result[path]=digest.hexdigest()
print(json.dumps(result,sort_keys=True))
'''


LISTENER_OWNERS = r'''
import errno,json,os,sys
port=int(sys.argv[1]);inodes=set();owners={};unreadable=[]
for name in ('tcp','tcp6'):
    try:
        with open('/proc/net/'+name) as stream:
            for line in list(stream)[1:]:
                parts=line.split()
                if parts[3]=='0A' and int(parts[1].rsplit(':',1)[1],16)==port:
                    inodes.add(parts[9])
    except OSError as e:
        if name!='tcp6' or e.errno!=errno.ENOENT:raise
for pid in os.listdir('/proc'):
    if not pid.isdigit():continue
    try:
        for fd in os.listdir('/proc/'+pid+'/fd'):
            try:link=os.readlink('/proc/'+pid+'/fd/'+fd)
            except OSError as e:
                if e.errno in (errno.ENOENT,errno.ESRCH):continue
                raise
            if link.startswith('socket:[') and link[8:-1] in inodes:
                owners.setdefault(link[8:-1],[]).append(int(pid))
    except OSError as e:
        if e.errno not in (errno.ENOENT,errno.ESRCH):unreadable.append(int(pid))
print(json.dumps({'inodes':sorted(inodes),'owners':owners,'unreadable':unreadable}))
'''


class Bootstrap:
    def __init__(self, manifest, ca, home, source, url, target_host, target_user, snapshot_file=None,
                 recover_degraded_controller=False):
        self.manifest, self.ca = manifest, ca
        self.home, self.source, self.url = os.path.abspath(home), os.path.abspath(source), url
        self.target_host, self.target_user = target_host, target_user
        self.snapshot_file = os.path.abspath(snapshot_file) if snapshot_file else None
        require(type(recover_degraded_controller) is bool, 'Invalid degraded recovery option')
        self.recover_degraded_controller = recover_degraded_controller
        self.db_path = os.path.join(self.home, 'gateway-' + manifest.get('site', '') + '.sqlite')
        self.paths = [self.db_path + suffix for suffix in ('', '-wal', '-shm')]
        self.stage = self.backup = None
        self.protected = {}
        self.units = {}
        self.previously_active = []
        self.originals = {}
        self.system_files = {}
        self.phase = 'not_started'
        self.failure_phase = None

    def progress(self, phase):
        self.phase = phase
        print('Mirror bootstrap: ' + phase, flush=True)

    def command(self, argv):
        try:
            return subprocess.check_output(argv, stderr=subprocess.STDOUT).decode('utf-8')
        except (OSError, subprocess.CalledProcessError):
            if 'systemctl' in argv:
                at = argv.index('systemctl')
                action = argv[at + 1]
                targets = [x for x in argv[at + 2:] if x in (CONTROLLER, GATEWAY)]
                raise Refused('systemctl ' + action + ' failed for ' + ', '.join(targets))
            raise Refused('Privileged file or process inspection failed during ' + self.phase)

    def unit(self, name):
        fields = ('LoadState', 'ActiveState', 'MainPID', 'User', 'WorkingDirectory', 'UnitFileState',
                  'ExecStart', 'FragmentPath', 'DropInPaths')
        args = ['systemctl', 'show'] + ['--property=' + key for key in fields] + [name]
        value = dict(line.split('=', 1) for line in self.command(args).splitlines() if '=' in line)
        # systemd also includes changing PID/time/exit fields in ExecStart.
        raw = value.get('ExecStart', '')
        argv = re.search(r'argv\[\]=([^;]+)', raw)
        executable = re.search(r'(?:^|[\s{])path=([^;]+)', raw)
        ignore = re.search(r'ignore_errors=([^;]+)', raw)
        require(argv and executable and ignore, 'Cannot verify service command')
        value['ExecStart'] = json.dumps({'argv': shlex.split(argv.group(1).strip()),
                                        'path': executable.group(1).strip(),
                                        'ignore_errors': ignore.group(1).strip()}, sort_keys=True)
        return value

    def system_hashes(self, paths):
        return json.loads(self.command(['sudo', '-n', sys.executable, '-B', '-S', '-c',
                                        HASH_SYSTEM_FILES] + sorted(paths)))

    def preflight(self):
        import pwd
        require(bool(self.url) != bool(self.snapshot_file), 'Choose exactly one snapshot source')
        validate_manifest(self.manifest, self.ca, needs_tls=not self.snapshot_file)
        require(self.target_host and self.target_user and pwd.getpwuid(os.getuid()).pw_name == self.target_user and
                socket.gethostname().split('.')[0].lower() == self.target_host.lower(), 'Unexpected target host or user')
        require(os.path.realpath(self.home) == self.home and os.stat(self.home).st_uid == os.getuid(),
                'Unexpected controller home')
        require(os.path.realpath(self.source) == self.source and os.path.isdir(self.source),
                'Expected installed source release')
        for name in ('node.json', 'node.key'):
            path = os.path.join(self.home, name)
            require(regular(path).st_uid == os.getuid(), 'Unexpected identity file owner')
            self.protected[path] = file_digest(path)
        with open(os.path.join(self.home, 'node.json'), encoding='utf-8') as stream:
            self.config = json.load(stream)
        require(self.config.get('site') == self.manifest['site'], 'Site mismatch')
        node = self.config.get('node', '')
        require(re.match(r'^[0-9a-f]{64}$', node) and node != self.manifest['source_node'],
                'Unexpected target node identity')
        settings = self.config.get('gateway_journal', {})
        require(settings.get('origin', '').rstrip('/') == self.manifest['gateway_origin'].rstrip('/'),
                'Gateway origin mismatch')
        require(isinstance(self.config.get('listen'), str) and self.config['listen'], 'Invalid listener address')
        require(type(self.config.get('port')) is int and 0 < self.config['port'] < 65536, 'Invalid controller port')
        system_paths = []
        for name in (CONTROLLER, GATEWAY):
            value = self.unit(name)
            require(value.get('LoadState') == 'loaded' and value.get('UnitFileState') in ('enabled', 'enabled-runtime')
                    and value.get('ActiveState') in ('active', 'inactive', 'failed'),
                    'Expected existing enabled, settled services')
            if value['ActiveState'] == 'active':
                self.previously_active.append(name)
            require(value.get('User') == self.target_user and value.get('WorkingDirectory') == self.source,
                    'Service deployment mismatch')
            start = json.loads(value['ExecStart'])
            if name == CONTROLLER:
                expected = ['/usr/bin/python3', '-S', '-m', 'biomerte', '--home', self.home, 'serve']
            else:
                expected = ['/usr/bin/python3', '-S', self.source + '/deploy/gateway-agent.py',
                            '--home', self.home, '--origin', self.manifest['gateway_origin'].rstrip('/')]
            require(start['argv'] == expected and start['path'] == expected[0] and
                    start['ignore_errors'] in ('no', 'false'), 'Unexpected service command')
            pid = int(value.get('MainPID', '0'))
            if pid:
                require(os.path.realpath('/proc/%d/cwd' % pid) == self.source, 'Running service source mismatch')
                running = process_argv('/proc/%d/cmdline' % pid)
                require(running == expected, 'Running service command mismatch')
            self.units[name] = {key: value.get(key) for key in
                                ('User', 'WorkingDirectory', 'ExecStart', 'FragmentPath', 'DropInPaths', 'UnitFileState')}
            for path in [value.get('FragmentPath', '')] + value.get('DropInPaths', '').split():
                require(path.startswith('/') and '\\' not in path, 'Unverifiable service unit path')
                system_paths.append(path)
        self.system_files = self.system_hashes(system_paths)
        require(set(self.system_files) == set(system_paths), 'Incomplete service file verification')
        position(self.db_path)
        if self.snapshot_file:
            regular(self.snapshot_file)
            require(self.snapshot_file not in self.paths and
                    self.snapshot_file not in self.protected and
                    self.snapshot_file != os.path.join(self.home, 'local.sqlite'),
                    'Snapshot input must not be an active node file')

    def disk_space(self, downloaded=False):
        old_size = 0
        for path in self.paths:
            try:
                old_size += regular(path).st_size
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
        # Existing files are moved, not copied. Retain an additional old-mirror
        # sized reserve for startup/WAL growth rather than using the last block.
        needed = old_size + (0 if downloaded else self.manifest['snapshot_bytes'])
        require(shutil.disk_usage(self.home).free >= needed, 'Insufficient free space for snapshot and retained cache')

    def unchanged(self):
        for path, digest in self.protected.items():
            require(file_digest(path) == digest, 'Identity or service configuration changed')
        require(self.system_hashes(self.system_files) == self.system_files, 'Service files changed')
        for name, expected in self.units.items():
            actual = self.unit(name)
            require(all(actual.get(key) == value for key, value in expected.items()),
                    'Effective service configuration changed')

    def probe(self):
        listen = self.config['listen']
        hosts = {'0.0.0.0': ['127.0.0.1'], '::': ['::1', '127.0.0.1'],
                 'localhost': ['127.0.0.1', '::1']}.get(listen, [listen])
        host = None
        for candidate in hosts:
            try:
                connection = socket.create_connection((candidate, self.config['port']), timeout=2)
                connection.close()
                host = candidate
                break
            except OSError as exc:
                if candidate == '::1' and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
                    continue
                require(exc.errno == errno.ECONNREFUSED, 'Controller listener status is uncertain')
        if host is None:
            return {'listening': False, 'healthy': False}
        healthy, degraded = False, False
        url = 'http://%s:%d/health' % ('[' + host + ']' if ':' in host else host, self.config['port'])
        try:
            with build_opener(ProxyHandler({}), NoRedirect).open(url, timeout=2) as response:
                value = json.loads(response.read(65536).decode('utf-8'))
            healthy = value.get('runtime') == 'up' and value.get('realms', {}).get('site') == 'ready'
            degraded = (value.get('runtime') == 'up' and value.get('realms', {}).get('local') == 'ready' and
                        value.get('realms', {}).get('site') == 'Gateway journal HTTP 503' and
                        value.get('background_errors') == {})
        except Exception:
            pass
        return {'listening': True, 'healthy': healthy, 'degraded': degraded}

    def verify_controller_listener(self):
        value = self.unit(CONTROLLER)
        pid = int(value.get('MainPID', '0'))
        require(value.get('ActiveState') == 'active' and pid > 0, 'Listener controller is not active')
        require(all(value.get(key) == item for key, item in self.units[CONTROLLER].items()),
                'Listener controller configuration changed')
        expected = json.loads(self.units[CONTROLLER]['ExecStart'])['argv']
        require(os.path.realpath('/proc/%d/cwd' % pid) == self.source and
                process_argv('/proc/%d/cmdline' % pid) == expected, 'Unexpected listener process')
        owners = json.loads(self.command(['sudo', '-n', sys.executable, '-B', '-S', '-c',
                                         LISTENER_OWNERS, str(self.config['port'])]))
        require(owners['inodes'] and not owners['unreadable'] and
                set(owners['owners']) == set(owners['inodes']) and
                all(set(pids) == {pid} for pids in owners['owners'].values()),
                'Listener ownership is not the expected controller')
        fresh = self.unit(CONTROLLER)
        require(fresh.get('MainPID') == str(pid) and fresh.get('ActiveState') == 'active',
                'Listener controller changed during verification')

    def no_longer_needed(self):
        health = self.probe()
        if health['listening']:
            if not self.recover_degraded_controller or health['healthy']:
                return 'controller_listening'
            require(health.get('degraded'), 'Listener is not a recognized degraded controller')
            self.verify_controller_listener()
        if position(self.db_path)[0] >= self.manifest['sequence']:
            return 'mirror_already_caught_up'
        return None

    def download(self, target):
        if self.snapshot_file:
            regular(self.snapshot_file)
            with open(self.snapshot_file, 'rb') as source:
                self.copy_snapshot(source, target)
            return
        address = urlsplit(self.url)
        require(address.scheme == 'https' and address.hostname and not address.username and
                not address.password and not address.query and not address.fragment, 'Invalid snapshot URL')
        ca_path = os.path.join(self.stage, 'snapshot-ca.pem')
        with open(ca_path, 'w') as stream:
            stream.write(self.ca)
        context = ssl.create_default_context(cafile=ca_path)
        require(context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname,
                'Verified TLS is required')
        opener = build_opener(ProxyHandler({}), NoRedirect, HTTPSHandler(context=context))
        with opener.open(Request(self.url), timeout=30) as response:
            self.copy_snapshot(response, target)

    def copy_snapshot(self, source, target):
        digest, size = hashlib.sha256(), 0
        with open(target, 'wb') as stream:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                require(size <= self.manifest['snapshot_bytes'], 'Snapshot exceeds declared size')
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        require(size == self.manifest['snapshot_bytes'] and
                digest.hexdigest() == self.manifest['snapshot_sha256'], 'Snapshot transfer verification failed')

    def verify_snapshot(self, target):
        require(os.stat(target).st_size == self.manifest['snapshot_bytes'] and
                file_digest(target) == self.manifest['snapshot_sha256'], 'Snapshot bytes changed')
        db = read_db(target)
        try:
            require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Snapshot integrity failed')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require({'records', 'position'} <= tables, 'Snapshot schema mismatch')
            require([r[1] for r in db.execute('PRAGMA table_info(records)')] == ['kind', 'id', 'value'],
                    'Snapshot record schema mismatch')
        finally:
            db.close()
        require(position(target) == (self.manifest['sequence'], self.manifest['hash']),
                'Snapshot anchor mismatch')

    def verify_remote(self):
        old_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.path.insert(0, self.source)
        try:
            module = importlib.import_module('biomerte.gateway_journal')
            store = importlib.import_module('biomerte.store')
            for value in (module, store):
                require(os.path.realpath(value.__file__).startswith(self.source + '/biomerte/'),
                        'Unexpected controller module source')
            class ReadOnlyJournal(module.GatewayJournal):
                def request(self, path, payload):
                    require(path in ('status', 'read'), 'Only journal reads are permitted')
                    with open(self.key_file) as stream:
                        token = stream.read().strip()
                    request = Request(self.origin + '/bridge/ha/' + path, store.encode(payload).encode('utf-8'),
                                      {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
                    opener = build_opener(ProxyHandler({}), NoRedirect,
                                          HTTPSHandler(context=ssl.create_default_context()))
                    with opener.open(request, timeout=self.timeout) as response:
                        return json.loads(response.read().decode('utf-8'))
            remote = ReadOnlyJournal(self.config['gateway_journal'], self.config['site'])
            require(remote.status().get('site') == self.manifest['site'], 'Authoritative site mismatch')
            head, unused = remote.read('head')
            seq, anchor = self.manifest['sequence'], self.manifest['hash']
            require(head and type(head.get('seq')) is int and head['seq'] >= seq,
                    'Authoritative journal is behind snapshot')
            entry, unused = remote.read('log/%020d' % seq)
            require(entry and entry.get('seq') == seq and store.digest(entry) == anchor,
                    'Authoritative snapshot anchor mismatch')
            require(head['seq'] != seq or head.get('hash') == anchor, 'Authoritative head mismatch')
        except Refused:
            raise
        except Exception:
            raise Refused('Authoritative snapshot verification failed')
        finally:
            sys.path.pop(0)
            sys.dont_write_bytecode = old_bytecode

    def stop(self):
        self.command(['sudo', '-n', 'systemctl', 'stop', GATEWAY, CONTROLLER])

    def quiescent(self):
        for name in (GATEWAY, CONTROLLER):
            value = self.unit(name)
            require(value.get('ActiveState') in ('inactive', 'failed') and value.get('MainPID') == '0',
                    'Services have not stopped')
        value = json.loads(self.command(['sudo', '-n', sys.executable, '-B', '-S', '-c', OWNER_SCAN] + self.paths))
        require(not value['owners'] and not value['unreadable'], 'Mirror ownership cannot be proven quiescent')

    def start(self):
        self.command(['sudo', '-n', 'systemctl', 'start', CONTROLLER])
        self.command(['sudo', '-n', 'systemctl', 'start', GATEWAY])

    def restore_services(self):
        for name in (CONTROLLER, GATEWAY):
            if name in self.previously_active:
                self.command(['sudo', '-n', 'systemctl', 'start', name])

    def wait_health(self):
        deadline = time.monotonic() + 120
        while True:
            value = self.probe()
            if value['healthy'] or time.monotonic() >= deadline:
                return value
            time.sleep(2)

    def run(self):
        self.progress('preflight')
        self.preflight()
        lock = os.open(self.home, os.O_RDONLY)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Refused('Another mirror bootstrap holds the home-directory lock')
            return self.run_locked()
        finally:
            os.close(lock)

    def record(self, phase):
        path = os.path.join(self.stage, 'recovery.json')
        with open(path + '.new', 'w') as stream:
            json.dump({'phase': phase, 'home': self.home, 'backup': self.backup,
                       'cache_files': [os.path.basename(p) for p in self.paths],
                       'previously_active': self.previously_active,
                       'original_files': self.originals,
                       'snapshot_sequence': self.manifest['sequence'],
                       'snapshot_sha256': self.manifest['snapshot_sha256']}, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path + '.new', path)
        sync_dir(self.stage)

    def run_locked(self):
        reason = self.no_longer_needed()
        if reason:
            return {'state': 'unchanged', 'reason': reason}
        self.disk_space()
        old_umask = os.umask(0o077)
        stopped, installed, replacement_started, keep = False, False, False, False
        candidate_identity = None
        try:
            self.stage = tempfile.mkdtemp(prefix='.mirror-bootstrap-', dir=self.home)
            sync_dir(self.home)
            candidate = os.path.join(self.stage, 'candidate.sqlite')
            self.progress('copying local snapshot' if self.snapshot_file else 'downloading snapshot')
            self.download(candidate)
            self.progress('verifying snapshot')
            self.verify_snapshot(candidate)
            self.verify_remote()
            self.progress('verified')
            self.unchanged()
            reason = self.no_longer_needed()
            if reason:
                return {'state': 'unchanged', 'reason': reason}
            self.disk_space(downloaded=True)
            self.progress('stopping services')
            stopped = True  # A failed stop command can still have stopped one unit.
            self.stop()
            self.quiescent()
            self.unchanged()
            if self.probe()['listening'] or position(self.db_path)[0] >= self.manifest['sequence']:
                self.restore_services()
                stopped = False
                return {'state': 'unchanged', 'reason': 'mirror_caught_up_before_stop'}
            self.verify_snapshot(candidate)
            self.disk_space(downloaded=True)
            self.backup = os.path.join(self.stage, 'previous-cache')
            os.mkdir(self.backup, 0o700)
            sync_dir(self.stage)
            expected = {}
            for path in self.paths:
                if os.path.lexists(path):
                    value = regular(path)
                    expected[path] = (value.st_dev, value.st_ino, value.st_size, value.st_mtime)
            self.originals = expected
            self.quiescent()
            require(not self.probe()['listening'], 'Controller listener appeared before replacement')
            keep = True  # Retain recovery material even on an interrupt during the first move.
            self.progress('backing up cache')
            self.record('moving_original_cache')
            for path in self.paths:
                require(os.path.lexists(path) == (path in expected), 'Cache file appeared or disappeared')
                if path in expected:
                    value = regular(path)
                    require((value.st_dev, value.st_ino, value.st_size, value.st_mtime) == expected[path],
                            'Cache file changed after quiescence')
                    saved = os.path.join(self.backup, os.path.basename(path))
                    os.rename(path, saved)
            sync_dir(self.home)
            sync_dir(self.backup)
            require(not any(os.path.lexists(path) for path in self.paths), 'Cache path was recreated')
            # Same-filesystem link is atomic and refuses to overwrite a raced-in database.
            value = regular(candidate)
            candidate_identity = (value.st_dev, value.st_ino)
            os.link(candidate, self.db_path)
            installed = True
            os.unlink(candidate)
            sync_dir(self.home)
            require(not any(os.path.lexists(path) for path in self.paths[1:]), 'Cache sidecar was recreated')
            self.quiescent()
            require(not self.probe()['listening'], 'Controller listener appeared before service start')
            self.unchanged()
            self.record('replacement_starting')
            replacement_started = True
            keep = True
            self.progress('restarting services')
            self.start()
            self.progress('checking health')
            result = self.wait_health()
            self.record('installed' if result['healthy'] else 'installed_not_ready')
            return {'state': 'installed' if result['healthy'] else 'installed_not_ready',
                    'listening': result['listening'], 'healthy': result['healthy'], 'backup': self.backup,
                    'position': position(self.db_path)[0]}
        except BaseException as failure:
            self.failure_phase = self.phase
            detail = str(failure) if isinstance(failure, Refused) else type(failure).__name__
            if replacement_started:
                keep = True
                raise Refused('Replacement start attempted; backup retained at ' + self.backup +
                              '; ' + detail + '; inspect services before any rollback')
            if stopped:
                try:
                    saved_paths = [(path, os.path.join(self.backup, os.path.basename(path)))
                                   for path in self.paths] if self.backup else []
                    # Derive moved files from disk, not bookkeeping that a signal
                    # could interrupt immediately after a successful rename.
                    moved = [(path, saved) for path, saved in saved_paths if os.path.lexists(saved)]
                    if moved or installed:
                        self.quiescent()
                    if candidate_identity and os.path.lexists(self.db_path):
                        value = os.lstat(self.db_path)
                        if (value.st_dev, value.st_ino) == candidate_identity:
                            os.rename(self.db_path, os.path.join(self.stage, 'unstarted-candidate.sqlite'))
                    for path in self.paths if self.originals else []:
                        if os.path.lexists(path):
                            value = regular(path)
                            require(path in self.originals and
                                    (value.st_dev, value.st_ino, value.st_size, value.st_mtime) == self.originals[path],
                                    'Unaccounted cache file blocks restoration')
                    for path, saved in reversed(moved):
                        require(not os.path.lexists(path), 'Cannot safely restore previous cache')
                        os.rename(saved, path)
                    for path, expected in self.originals.items():
                        value = regular(path)
                        require((value.st_dev, value.st_ino, value.st_size, value.st_mtime) == expected,
                                'Restored cache identity mismatch')
                    sync_dir(self.home)
                    self.unchanged()
                    self.restore_services()
                    keep = False
                except BaseException as recovery_failure:
                    keep = True
                    recovery_detail = str(recovery_failure) if isinstance(recovery_failure, Refused) else type(recovery_failure).__name__
                    raise Refused('Recovery incomplete; retained files at ' + self.stage +
                                  '; ' + recovery_detail + '; inspect service and cache state')
                raise Refused('Bootstrap failed before replacement start; ' + detail +
                              '; original cache and services restored')
            raise
        finally:
            if self.stage and not keep:
                shutil.rmtree(self.stage)
            os.umask(old_umask)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise Refused('Bootstrap interrupted')

    bootstrap = None
    try:
        require(len(sys.argv) == 2, 'A reviewed preparation configuration file is required')
        with open(sys.argv[1], encoding='utf-8') as stream:
            preparation = json.load(stream)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        bootstrap = Bootstrap(preparation['manifest'], preparation.get('ca_pem', ''), preparation['home'],
                              preparation['source'], preparation.get('download_url', ''), preparation['target_host'],
                              preparation['target_user'], preparation.get('snapshot_file'),
                              preparation.get('recover_degraded_controller', False))
        result = bootstrap.run()
        print(json.dumps(result, sort_keys=True))
        if result['state'] == 'installed_not_ready':
            sys.exit(2)
    except Exception as exc:
        # Never print raw network/SQLite exceptions, credentials, or records.
        print(json.dumps({'state': 'refused', 'phase': (bootstrap.failure_phase or bootstrap.phase) if bootstrap else 'configuration',
                          'reason': str(exc) if isinstance(exc, Refused)
                          else 'Bootstrap failed before verified completion'}))
        sys.exit(1)
