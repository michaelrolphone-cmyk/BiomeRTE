#!/usr/bin/env python3
"""Opt-in rolling retention for declared Biome launchd text logs.

Plan is read-only. Apply requires an exact manifest digest. It never installs a
job, changes a plist, signals a process, or touches operational record stores.
Copy-truncate has a concurrent-write loss window; see docs/service-log-retention.md.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import stat
import subprocess
import sys
import time

CHUNK = 65536
LABEL = re.compile(r'^org\.biomerte\.[A-Za-z0-9._-]+$')
DENIED_PARTS = {'jobs', 'commands', 'artifacts', 'journals', 'receipts', 'workspace'}
NOFOLLOW = os.O_NOFOLLOW
DIRECTORY = os.O_DIRECTORY


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def canonical(path):
    require(isinstance(path, str) and os.path.isabs(path), 'Expected absolute path')
    require(os.path.normpath(path) == path and '\x00' not in path, 'Noncanonical path')
    return path


def secure_dir(path, uid, private=False, create=False):
    """Pin every directory component; never traverse a symlink."""
    canonical(path)
    fd = os.open('/', os.O_RDONLY | DIRECTORY)
    try:
        for part in path.split('/')[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        require(info.st_uid == uid, 'Directory owner differs: ' + path)
        require(not info.st_mode & 0o022, 'Directory is writable by others: ' + path)
        if private:
            require(not info.st_mode & 0o077, 'State directory is not private: ' + path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def regular(fd, uid, private=False):
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1,
            'Expected singly linked regular file owned by the service user')
    require(not info.st_mode & 0o022, 'File is writable by others')
    if private:
        require(not info.st_mode & 0o077, 'State file is not private')
    return info


def file_at(directory, name, uid, flags=os.O_RDONLY, private=False):
    require('/' not in name and name not in ('', '.', '..'), 'Invalid file name')
    fd = os.open(name, flags | NOFOLLOW, 0o600, dir_fd=directory)
    try:
        regular(fd, uid, private)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_fd(fd, limit=None):
    os.lseek(fd, 0, os.SEEK_SET)
    out = []
    size = 0
    while True:
        block = os.read(fd, CHUNK)
        if not block:
            break
        size += len(block)
        require(limit is None or size <= limit, 'Metadata exceeds size limit')
        out.append(block)
    return b''.join(out)


def read_private(directory, name, uid):
    fd = file_at(directory, name, uid, private=True)
    try:
        return read_fd(fd, 1024 * 1024)
    finally:
        os.close(fd)


def write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        require(n > 0, 'Short write')
        view = view[n:]


def atomic_private(directory, name, value, uid):
    """One fixed, bounded staging name per destination, protected by global lock."""
    temp = '.' + name + '.pending'
    fd = file_at(directory, temp, uid, os.O_RDWR | os.O_CREAT, private=True)
    try:
        os.ftruncate(fd, 0)
        write_all(fd, value)
        os.fsync(fd)
    finally:
        os.close(fd)
    # Refuse symlinks/hard links or another owner's file even though replace
    # would not dereference them. Never silently repurpose an unrelated entry.
    try:
        old = file_at(directory, name, uid, private=True)
    except FileNotFoundError:
        pass
    else:
        os.close(old)
    os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)


def safe_log_path(path, home):
    canonical(path)
    prefix = home + '/.local/share/'
    require(path.startswith(prefix), 'Log is outside Biome service data directories')
    parts = path[len(prefix):].split('/')
    require(len(parts) >= 2 and re.match(r'^biome(?:rte)?[-A-Za-z0-9._]*$', parts[0]),
            'Log is outside a Biome service directory')
    require(parts[0] != 'biomerte-service-log-retention', 'Retention state is not a service log')
    require(not DENIED_PARTS.intersection(parts[1:-1]), 'Operational output directory is excluded')
    require(parts[-1].endswith(('.log', '.err')), 'Not a plain service text-log sink')
    return path


def read_plist(path):
    canonical(path)
    # Installed service configuration may be root-owned, but cannot be a link
    # or group/world writable. Never run or modify it.
    require(os.path.realpath(path) == path, 'Symlink in plist path')
    fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o022,
                'Unsafe service plist')
        raw = read_fd(fd, 1024 * 1024)
    finally:
        os.close(fd)
    return plistlib.loads(raw), raw


def plan(home, plist_paths, max_bytes=1024 * 1024, generations=2):
    home = canonical(home)
    uid = os.getuid()
    parent = secure_dir(home, uid)
    os.close(parent)
    require(type(max_bytes) is int and 1 <= max_bytes <= 64 * 1024 * 1024, 'Invalid byte limit')
    require(type(generations) is int and 1 <= generations <= 16, 'Invalid generation count')
    targets = {}
    services = []
    for path in sorted(set(plist_paths)):
        job, raw = read_plist(path)
        label = job.get('Label', '')
        if not LABEL.fullmatch(label):
            continue
        entries = []
        for key in ('StandardOutPath', 'StandardErrorPath'):
            sink = job.get(key)
            if sink in (None, '/dev/null'):
                continue
            safe_log_path(sink, home)
            entries.append(sink)
            targets.setdefault(sink, []).append({'label': label, 'stream': key})
        if entries:
            services.append({'path': path, 'label': label, 'sha256': digest(raw)})
    require(targets, 'No eligible Biome service text logs')
    return {'version': 1, 'home': home, 'uid': uid,
            'state_dir': home + '/.local/share/biomerte-service-log-retention',
            'max_bytes': max_bytes, 'generations': generations, 'services': services,
            'targets': [{'path': p, 'uses': targets[p]} for p in sorted(targets)]}


def validate_manifest(manifest, expected):
    require(digest(encoded(manifest)) == expected, 'Manifest digest differs')
    require(manifest.get('version') == 1 and manifest.get('uid') == os.getuid(), 'Manifest user/version differs')
    rebuilt = plan(manifest['home'], [s['path'] for s in manifest['services']],
                   manifest['max_bytes'], manifest['generations'])
    require(rebuilt == manifest, 'Installed service configuration or manifest scope changed')
    return manifest


def parse_writers(text, path, source):
    """Parse macOS lsof +fg's named flags, including inherited/shared FDs."""
    lines = text.splitlines()
    require(lines and lines[0].split() ==
            ['COMMAND', 'PID', 'USER', 'FD', 'TYPE', 'FILE-FLAG', 'DEVICE', 'SIZE/OFF', 'NODE', 'NAME'],
            'Unrecognized lsof output')
    writers = []
    for line in lines[1:]:
        row = line.split(None, 9)
        require(len(row) == 10 and row[4] == 'REG' and row[9] == path,
                'Incomplete or mismatched lsof descriptor')
        require(row[8].isdigit() and int(row[8]) == source.st_ino, 'Writer inode differs')
        require(row[6] == '{},{}'.format(os.major(source.st_dev), os.minor(source.st_dev)),
                'Writer device differs')
        require(re.match(r'^\d+[rwu]$', row[3]), 'Unknown descriptor access, possibly a memory mapping')
        if row[3][-1] == 'r':
            continue
        flags = set(re.split('[,;]', row[5]))
        require('AP' in flags, 'Active writer lacks O_APPEND')
        writers.append({'pid': int(row[1]), 'fd': int(row[3][:-1])})
    return writers


def inspect_writers(path, source):
    require(sys.platform == 'darwin', 'Native writer inspection requires macOS')
    proc = subprocess.Popen(['/usr/sbin/lsof', '-nP', '+fg', '--', path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise ValueError('Writer inspection timed out')
    require(not err, 'Writer inspection is incomplete: ' + err.decode('utf-8', 'replace')[:160])
    # The rotator itself holds an append-open writable FD. No selected files
    # therefore means the observation is incomplete, not proof of no writers.
    require(proc.returncode == 0, 'Writer inspection failed')
    writers = parse_writers(out.decode('utf-8', 'strict'), path, source)
    require(any(w['pid'] == os.getpid() for w in writers), 'Writer inspection cannot see the rotator itself')
    return writers


def copy_range(source, target, start, count):
    os.lseek(source, start, os.SEEK_SET)
    hashed = hashlib.sha256()
    while count:
        block = os.read(source, min(CHUNK, count))
        require(block, 'Source shortened during copy')
        if target is not None:
            write_all(target, block)
        hashed.update(block)
        count -= len(block)
    return hashed.hexdigest()


def verify_baseline(source, directory, path, uid, manifest_digest=None):
    """Preserve a verified full historical prefix exactly once; never prune it."""
    info = regular(source, uid)
    try:
        metadata = json.loads(read_private(directory, 'baseline.json', uid).decode('utf-8'))
    except FileNotFoundError:
        # A crash before baseline metadata was durable cannot authorize truncation.
        try:
            fd = file_at(directory, 'baseline.log', uid, private=True)
        except FileNotFoundError:
            pass
        else:
            os.close(fd)
            raise ValueError('Uncommitted baseline exists; reconcile without deleting history')
        temp = file_at(directory, '.baseline.pending', uid, os.O_RDWR | os.O_CREAT, private=True)
        try:
            os.ftruncate(temp, 0)
            sha = copy_range(source, temp, 0, info.st_size)
            os.fsync(temp)
            require(copy_range(temp, None, 0, info.st_size) == sha, 'Baseline archive verification failed')
            after = regular(source, uid)
            require((after.st_dev, after.st_ino) == (info.st_dev, info.st_ino)
                    and after.st_size >= info.st_size, 'Source changed during baseline archive')
            require(copy_range(source, None, 0, info.st_size) == sha, 'Historical source prefix changed')
        finally:
            os.close(temp)
        # Hard-link creation is no-clobber, then remove only our staging name.
        os.link('.baseline.pending', 'baseline.log', src_dir_fd=directory, dst_dir_fd=directory,
                follow_symlinks=False)
        os.unlink('.baseline.pending', dir_fd=directory)
        os.fsync(directory)
        metadata = {'path': path, 'dev': info.st_dev, 'ino': info.st_ino,
                    'bytes': info.st_size, 'sha256': sha, 'at': time.time()}
        atomic_private(directory, 'baseline.json', encoded(metadata), uid)
    require(metadata['path'] == path and (metadata['dev'], metadata['ino']) == (info.st_dev, info.st_ino),
            'Source identity differs from preserved baseline')
    retirement = metadata.get('retirement')
    if retirement is not None:
        require(retirement.get('version') == 1 and retirement.get('state') in ('retiring', 'retired')
                and retirement.get('sha256') == metadata['sha256']
                and retirement.get('bytes') == metadata['bytes']
                and (manifest_digest is None or retirement.get('manifest_sha256') == manifest_digest), 'Invalid baseline retirement fence')
        require(retirement['state'] == 'retired', 'Baseline retirement is pending; resume its approved plan')
        try:
            unexpected = file_at(directory, 'baseline.log', uid, private=True)
        except FileNotFoundError:
            return metadata
        else:
            os.close(unexpected)
            raise ValueError('Retired baseline reappeared')
    baseline = file_at(directory, 'baseline.log', uid, private=True)
    try:
        require(os.fstat(baseline).st_size == metadata['bytes'] and
                copy_range(baseline, None, 0, metadata['bytes']) == metadata['sha256'], 'Baseline is not verified')
    finally:
        os.close(baseline)
    return metadata


def source_still_named(directory, name, source, uid):
    probe = file_at(directory, name, uid)
    try:
        a, b = os.fstat(probe), os.fstat(source)
        require((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino), 'Source path changed')
    finally:
        os.close(probe)


def rotate(source, source_dir, name, directory, path, uid, max_bytes, generations, writer_check):
    info = regular(source, uid)
    if info.st_size <= max_bytes:
        return {'path': path, 'state': 'within_limit', 'bytes': info.st_size}
    writer_check(path, info)
    # Slots are fixed in number and each stores at most max_bytes. A fixed staging
    # file also stays bounded across crashes; no timestamp-named orphan growth.
    slots = []
    for n in range(generations):
        try:
            metadata = json.loads(read_private(directory, 'generation-{}.json'.format(n), uid).decode('utf-8'))
            sequence = metadata['sequence']
            require(type(sequence) is int and sequence >= 0, 'Invalid generation metadata')
        except FileNotFoundError:
            sequence = -1
        slots.append((sequence, n))
    sequence = max(s[0] for s in slots) + 1
    slot = min(slots)[1]
    temp = file_at(directory, '.generation.pending', uid, os.O_RDWR | os.O_CREAT, private=True)
    try:
        os.ftruncate(temp, 0)
        start = max(0, info.st_size - max_bytes)
        size = info.st_size - start
        sha = copy_range(source, temp, start, size)
        os.fsync(temp)
        require(copy_range(temp, None, 0, size) == sha, 'Rolling copy verification failed')
    finally:
        os.close(temp)
    source_still_named(source_dir, name, source, uid)
    before = regular(source, uid)
    require(before.st_size >= info.st_size, 'Source shrank during rotation')
    writer_check(path, before)
    target = 'generation-{}.log'.format(slot)
    try:
        old = file_at(directory, target, uid, private=True)
    except FileNotFoundError:
        pass
    else:
        require(os.fstat(old).st_size <= max_bytes, 'Existing generation exceeds managed bound')
        os.close(old)
    os.replace('.generation.pending', target, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)
    atomic_private(directory, 'generation-{}.json'.format(slot), encoded(
        {'sequence': sequence, 'bytes': size, 'sha256': sha, 'source_end': info.st_size, 'at': time.time()}), uid)
    source_still_named(source_dir, name, source, uid)
    # Last check is deliberately immediately before truncation. It cannot lock
    # cooperating/noncooperating writers atomically; the documented race remains.
    writer_check(path, regular(source, uid))
    os.ftruncate(source, 0)
    os.fsync(source)
    return {'path': path, 'state': 'rotated', 'captured_bytes': size,
            'captured_through': info.st_size, 'generation': slot, 'copytruncate_loss_window': True}


@contextlib.contextmanager
def target_handles(manifest, path):
    uid = manifest['uid']
    source_dir = secure_dir(os.path.dirname(path), uid)
    source = directory = None
    try:
        # lsof sees the rotator too. Its own writable descriptor must satisfy the
        # same append guard as every service writer; ftruncate still works.
        source = file_at(source_dir, os.path.basename(path), uid, os.O_RDWR | os.O_APPEND)
        directory = secure_dir(manifest['state_dir'] + '/' + digest(path.encode('utf-8')),
                               uid, private=True, create=True)
        yield source, source_dir, directory
    finally:
        if source is not None:
            os.close(source)
        if directory is not None:
            os.close(directory)
        os.close(source_dir)


def apply(manifest, expected, writer_check=inspect_writers):
    manifest = validate_manifest(manifest, expected)
    uid = manifest['uid']
    state = secure_dir(manifest['state_dir'], uid, private=True, create=True)
    lock = None
    try:
        lock = file_at(state, 'lock', uid, os.O_RDWR | os.O_CREAT, private=True)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        value = encoded(manifest)
        try:
            require(read_private(state, 'manifest.json', uid) == value, 'State belongs to another manifest')
        except FileNotFoundError:
            atomic_private(state, 'manifest.json', value, uid)
        # Complete the full baseline pass before ANY source is truncated, but
        # close each target immediately: descriptors do not scale with inventory.
        for target in manifest['targets']:
            path = target['path']
            with target_handles(manifest, path) as (source, source_dir, directory):
                verify_baseline(source, directory, path, uid, digest(encoded(manifest)))
        results = []
        for target in manifest['targets']:
            path = target['path']
            try:
                with target_handles(manifest, path) as (source, source_dir, directory):
                    # Reopening cannot silently admit a replacement inode.
                    verify_baseline(source, directory, path, uid, digest(encoded(manifest)))
                    results.append(rotate(source, source_dir, os.path.basename(path), directory, path,
                                          uid, manifest['max_bytes'], manifest['generations'], writer_check))
            except (ValueError, OSError) as error:
                results.append({'path': path, 'state': 'blocked', 'error': str(error)})
        report = {'at': time.time(), 'manifest_sha256': expected, 'results': results,
                  'ok': all(x['state'] != 'blocked' for x in results)}
        atomic_private(state, 'status.json', encoded(report), uid)
        return report
    finally:
        if lock is not None:
            os.close(lock)
        os.close(state)



@contextlib.contextmanager
def baseline_handles(manifest, path):
    """Read only existing source/state; planning never creates missing paths."""
    uid = manifest['uid']
    source_dir = secure_dir(os.path.dirname(path), uid)
    source = directory = None
    try:
        source = file_at(source_dir, os.path.basename(path), uid)
        directory = secure_dir(manifest['state_dir'] + '/' + digest(path.encode()), uid, private=True)
        yield source, source_dir, directory
    finally:
        for fd in (source, directory, source_dir):
            if fd is not None:
                os.close(fd)


def baseline_candidate(manifest, path):
    uid = manifest['uid']
    with baseline_handles(manifest, path) as (source, source_dir, directory):
        raw = read_private(directory, 'baseline.json', uid)
        metadata = json.loads(raw)
        if metadata.get('retirement', {}).get('state') == 'retired':
            verify_baseline(source, directory, path, uid, digest(encoded(manifest)))
            return {'path': path, 'already_retired': True}
        require('retirement' not in metadata, 'Baseline retirement pending; resume its approved plan')
        require(type(metadata.get('at')) in (int, float) and math.isfinite(metadata['at'])
                and metadata['at'] >= 0, 'Original baseline timestamp is invalid')
        # Existing metadata is required before this call; never create an archive
        # merely to decide whether an old archive can roll off.
        verify_baseline(source, directory, path, uid, digest(encoded(manifest)))
        source_info = regular(source, uid)
        baseline = file_at(directory, 'baseline.log', uid, private=True)
        try:
            info = regular(baseline, uid, private=True)
            identity = {'dev': info.st_dev, 'ino': info.st_ino, 'bytes': info.st_size,
                        'sha256': copy_range(baseline, None, 0, info.st_size)}
        finally:
            os.close(baseline)
        generations = []
        for slot in range(manifest['generations']):
            name = 'generation-{}'.format(slot)
            try:
                body = read_private(directory, name + '.json', uid)
            except FileNotFoundError:
                try:
                    orphan = file_at(directory, name + '.log', uid, private=True)
                except FileNotFoundError:
                    continue
                else:
                    os.close(orphan)
                    raise ValueError('Uncommitted rolling generation')
            generation = json.loads(body)
            require(type(generation.get('sequence')) is int and generation['sequence'] >= 0
                    and type(generation.get('bytes')) is int
                    and 0 <= generation['bytes'] <= manifest['max_bytes']
                    and type(generation.get('at')) in (int, float) and math.isfinite(generation['at'])
                    and generation['at'] >= 0
                    and type(generation.get('source_end')) is int
                    and generation['source_end'] >= generation['bytes'], 'Invalid rolling generation')
            fd = file_at(directory, name + '.log', uid, private=True)
            try:
                require(os.fstat(fd).st_size == generation['bytes'] and
                        copy_range(fd, None, 0, generation['bytes']) == generation['sha256'], 'Rolling generation is not verified')
            finally:
                os.close(fd)
            generations.append({'slot': slot, 'metadata_sha256': digest(body), **generation})
        # A baseline is an extra diagnostic copy, not an extra rolling slot. Its
        # recent tail is already covered by an authenticated managed rotation, or
        # its original prefix is still present in the unchanged active source.
        rotated = any(type(g.get('at')) in (int, float) and type(metadata.get('at')) in (int, float)
                      and g['at'] >= metadata['at'] for g in generations)
        if not rotated:
            require(source_info.st_size >= metadata['bytes'] and
                    copy_range(source, None, 0, metadata['bytes']) == metadata['sha256'],
                    'Neither verified rotation nor intact original source covers baseline')
        source_still_named(source_dir, os.path.basename(path), source, uid)
        return {'path': path, 'metadata_sha256': digest(raw), 'baseline': identity,
                'source': {'dev': source_info.st_dev, 'ino': source_info.st_ino},
                'generations': generations, 'coverage': 'rolling_generations' if rotated else 'original_source'}


def baseline_plan(manifest, expected, after='', limit=64):
    validate_manifest(manifest, expected)
    require(isinstance(after, str) and type(limit) is int and 1 <= limit <= 128, 'Invalid baseline migration range')
    uid = manifest['uid']
    state = secure_dir(manifest['state_dir'], uid, private=True)
    lock = None
    try:
        require(read_private(state, 'manifest.json', uid) == encoded(manifest), 'State belongs to another manifest')
        lock = file_at(state, 'lock', uid, private=True)
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        paths = [t['path'] for t in manifest['targets'] if t['path'] > after][:limit]
        entries, blocked, already = [], [], []
        for path in paths:
            try:
                candidate = baseline_candidate(manifest, path)
                if candidate.get('already_retired'):
                    already.append(path)
                else:
                    entries.append(candidate)
            except (OSError, ValueError, KeyError, TypeError) as error:
                blocked.append({'path': path, 'reason': str(error)})
        plan = {'version': 1, 'manifest_sha256': expected, 'after': after,
                'next_cursor': paths[-1] if paths else after, 'more': len(paths) == limit,
                'entries': entries, 'blocked': blocked, 'already_retired': already,
                'bytes_to_retire': sum(e['baseline']['bytes'] for e in entries)}
        return {'plan': plan, 'plan_sha256': digest(encoded(plan))}
    finally:
        if lock is not None:
            os.close(lock)
        os.close(state)


def baseline_apply(manifest, expected, plan, expected_plan, after_intent=None, after_unlink=None):
    validate_manifest(manifest, expected)
    require(isinstance(plan, dict) and plan.get('version') == 1 and plan.get('manifest_sha256') == expected
            and digest(encoded(plan)) == expected_plan and isinstance(plan.get('entries'), list)
            and len(plan['entries']) <= 128, 'Baseline retirement plan digest or scope differs')
    allowed = {target['path'] for target in manifest['targets']}
    require(len({e['path'] for e in plan['entries']}) == len(plan['entries'])
            and all(e['path'] in allowed for e in plan['entries']), 'Baseline retirement targets differ')
    uid = manifest['uid']
    state = secure_dir(manifest['state_dir'], uid, private=True)
    lock = None
    retired, already, blocked = [], [], []
    try:
        require(read_private(state, 'manifest.json', uid) == encoded(manifest), 'State belongs to another manifest')
        lock = file_at(state, 'lock', uid, private=True)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for entry in plan['entries']:
            path = entry['path']
            try:
                with baseline_handles(manifest, path) as (source, source_dir, directory):
                    raw = read_private(directory, 'baseline.json', uid)
                    metadata = json.loads(raw)
                    info = regular(source, uid)
                    require({'dev': info.st_dev, 'ino': info.st_ino} == entry['source'], 'Source identity changed')
                    intent = metadata.get('retirement')
                    if intent is None:
                        require(baseline_candidate(manifest, path) == entry, 'Baseline retirement preimage changed')
                        intent = {'version': 1, 'state': 'retiring', 'manifest_sha256': expected,
                                  'plan_sha256': expected_plan, **entry['baseline']}
                        metadata['retirement'] = intent
                        atomic_private(directory, 'baseline.json', encoded(metadata), uid)
                        if after_intent:
                            after_intent(path)
                    require(intent.get('manifest_sha256') == expected and intent.get('plan_sha256') == expected_plan
                            and all(intent.get(k) == v for k, v in entry['baseline'].items()), 'Baseline retirement intent differs')
                    if intent.get('state') == 'retired':
                        verify_baseline(source, directory, path, uid, digest(encoded(manifest)))
                        already.append(path)
                        continue
                    require(intent.get('state') == 'retiring', 'Invalid baseline retirement state')
                    try:
                        archive = file_at(directory, 'baseline.log', uid, private=True)
                    except FileNotFoundError:
                        # Only this durable exact-file intent authorizes completing
                        # a crash after unlink without recreating the baseline.
                        pass
                    else:
                        try:
                            info = regular(archive, uid, private=True)
                            actual = {'dev': info.st_dev, 'ino': info.st_ino, 'bytes': info.st_size,
                                      'sha256': copy_range(archive, None, 0, info.st_size)}
                            require(actual == entry['baseline'], 'Baseline changed before retirement')
                            named = os.stat('baseline.log', dir_fd=directory, follow_symlinks=False)
                            require((named.st_dev, named.st_ino) == (info.st_dev, info.st_ino), 'Baseline path replaced')
                            os.unlink('baseline.log', dir_fd=directory)
                            os.fsync(directory)
                            if after_unlink:
                                after_unlink(path)
                        finally:
                            os.close(archive)
                    intent['state'] = 'retired'
                    atomic_private(directory, 'baseline.json', encoded(metadata), uid)
                    retired.append(path)
            except (OSError, ValueError, KeyError, TypeError) as error:
                blocked.append({'path': path, 'reason': str(error)})
        return {'retired': retired, 'already_retired': already, 'blocked': blocked,
                'ok': not blocked, 'bytes_retired': sum(e['baseline']['bytes'] for e in plan['entries'] if e['path'] in retired)}
    finally:
        if lock is not None:
            os.close(lock)
        os.close(state)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action')
    p = sub.add_parser('plan')
    p.add_argument('--home', default=os.path.expanduser('~'))
    p.add_argument('--plists', nargs='+', required=True, help='Existing verified launchd plist paths')
    p.add_argument('--max-bytes', type=int, default=1024 * 1024)
    p.add_argument('--generations', type=int, default=2)
    for action in ('digest', 'apply'):
        p = sub.add_parser(action)
        p.add_argument('--manifest', required=True)
        if action == 'apply':
            p.add_argument('--expected-sha256', required=True)
    for action in ('baseline-plan', 'baseline-apply'):
        p = sub.add_parser(action)
        p.add_argument('--manifest', required=True)
        p.add_argument('--expected-sha256', required=True)
        p.add_argument('--after', default='')
        p.add_argument('--limit', type=int, default=64)
        if action == 'baseline-apply':
            p.add_argument('--plan', required=True)
            p.add_argument('--expected-plan-sha256', required=True)
    options = parser.parse_args()
    if options.action == 'plan':
        print(json.dumps(plan(options.home, options.plists, options.max_bytes, options.generations), indent=2))
    elif options.action in ('digest', 'apply'):
        with open(options.manifest) as f:
            manifest = json.load(f)
        if options.action == 'digest':
            print(digest(encoded(manifest)))
        else:
            result = apply(manifest, options.expected_sha256)
            print(json.dumps(result))
            return 0 if result['ok'] else 1
    elif options.action in ('baseline-plan', 'baseline-apply'):
        with open(options.manifest) as f:
            manifest = json.load(f)
        if options.action == 'baseline-plan':
            result = baseline_plan(manifest, options.expected_sha256, options.after, options.limit)
        else:
            with open(options.plan) as f:
                saved = json.load(f)
            result = baseline_apply(manifest, options.expected_sha256, saved['plan'], options.expected_plan_sha256)
        print(json.dumps(result))
    else:
        parser.error('Choose plan, digest, apply, baseline-plan or baseline-apply')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
