"""Future-only acknowledged maintenance diagnostics. Execution fences never expire."""
import hashlib
import io
import json
import math
import os
import selectors
import stat
import subprocess
import sys
import time

DEFAULTS = {'output_count': 100, 'output_bytes': 64 * 1024 * 1024,
            'result_count': 1000, 'result_bytes': 4 * 1024 * 1024,
            'max_age_seconds': 7 * 24 * 60 * 60}
LSOF_OUTPUT_LIMIT = 64 * 1024
LSOF_TIMEOUT_SECONDS = 15


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def policy(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unknown maintenance output-retention setting')
    merged = dict(DEFAULTS, **value)
    if any(type(v) is not int or v < 0 for v in merged.values()):
        raise ValueError('Retention limits must be nonnegative integers')
    return merged


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS maintenance_output_retention('
               'id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,created REAL NOT NULL,'
               'completed REAL,result_digest TEXT,acked REAL,output_manifest TEXT,'
               'outputs_retired INTEGER NOT NULL DEFAULT 0,result_retired INTEGER NOT NULL DEFAULT 0)')


def admit(db, job, now=None):
    """Call only for authenticated device deliveries. Never enroll old rows."""
    now = time.time() if now is None else now
    identity = sha({'argv': job['argv'], 'cwd': job.get('cwd')})
    with db:
        prior = db.execute('SELECT state FROM commands WHERE id=?', (job['id'],)).fetchone()
        if prior is not None:
            marker = db.execute('SELECT request_digest FROM maintenance_output_retention WHERE id=?',
                                (job['id'],)).fetchone()
            if marker and marker[0] != identity:
                raise ValueError('Maintenance command identity conflict')
            return False
        db.execute("INSERT INTO commands VALUES(?,'running',NULL)", (job['id'],))
        db.execute('INSERT INTO maintenance_output_retention(id,request_digest,created) VALUES(?,?,?)',
                   (job['id'], identity, now))
    return True


def recover_running(db):
    result = {'state': 'uncertain', 'reason': 'Maintenance agent restarted; reconcile process and output files before another execution.'}
    with db:
        for identifier, in db.execute("SELECT id FROM commands WHERE state='running'").fetchall():
            db.execute("UPDATE commands SET state='uncertain',result=? WHERE id=?", (json.dumps(result), identifier))
            db.execute('UPDATE maintenance_output_retention SET completed=NULL,result_digest=? WHERE id=?',
                       (sha(result), identifier))


def process_group_finished(pid):
    """A direct child exit alone does not establish that its descendants stopped."""
    try:
        os.killpg(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except OSError:
        return False


def complete(db, identifier, result, quiescent, now=None, home=None):
    now = time.time() if now is None else now
    raw = json.dumps(result)
    eligible = bool(quiescent and home is not None and result.get('state') in ('succeeded', 'failed')
                    and all(isinstance(result.get(name), str) for name in ('stdout', 'stderr')))
    outputs = None
    if eligible:
        try:
            outputs = capture_outputs(home, identifier, result)
        except (OSError, ValueError):
            eligible = False
    with db:
        row = db.execute('SELECT state FROM commands WHERE id=?', (identifier,)).fetchone()
        if row is None or row[0] != 'running':
            raise ValueError('Maintenance command is no longer running')
        db.execute('UPDATE commands SET state=?,result=? WHERE id=?', (result['state'], raw, identifier))
        db.execute('UPDATE maintenance_output_retention SET completed=?,result_digest=?,output_manifest=? WHERE id=?',
                   (now if eligible else None, sha(result), json.dumps(outputs) if outputs is not None else None, identifier))


def acknowledge(db, identifier, sent, receipt, host, now=None):
    """Legacy accepted=true remains legacy behavior; future cleanup needs exact ACK."""
    now = time.time() if now is None else now
    digest = sha(sent)
    with db:
        row = db.execute('SELECT state,result FROM commands WHERE id=?', (identifier,)).fetchone()
        if row is None or row[0] not in ('succeeded', 'failed', 'uncertain', 'reported'):
            return False
        marker = db.execute('SELECT result_digest,result_retired FROM maintenance_output_retention WHERE id=?',
                            (identifier,)).fetchone()
        exact = isinstance(receipt, dict) and receipt.get('accepted') is True and \
                receipt.get('id') == identifier and receipt.get('host') == host and \
                receipt.get('result_sha256') == digest
        if marker is None:
            if not isinstance(receipt, dict) or receipt.get('accepted') is not True:
                return False
            # A pre-upgrade command is never added to the eligibility table.
            if row[1] is None or sha(json.loads(row[1])) != digest:
                return False
            db.execute("UPDATE commands SET state='reported' WHERE id=?", (identifier,))
            return True
        if not exact or marker[0] != digest:
            return False
        if marker[1]:
            value = json.loads(row[1])
            return row[0] == 'reported' and value.get('result_retired') is True and \
                   value.get('result_sha256') == digest
        if row[1] is None or sha(json.loads(row[1])) != digest:
            return False
        db.execute("UPDATE commands SET state='reported' WHERE id=?", (identifier,))
        db.execute('UPDATE maintenance_output_retention SET acked=COALESCE(acked,?) WHERE id=?',
                   (now, identifier))
    return True


def output_dir(home, identifier):
    """Pin no-follow directories so cleanup cannot escape into user files."""
    home = os.path.abspath(str(home))
    if os.path.realpath(home) != home:
        raise ValueError('Symlink in maintenance home')
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in ('commands', hashlib.sha256(identifier.encode()).hexdigest()):
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError('Unsafe maintenance output directory')
            new = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Unsafe maintenance output directory')
        return fd
    except BaseException:
        os.close(fd)
        raise


def lsof_pins_output(out):
    """Only complete, numeric, read-only FDs of this process are exempt."""
    if not out or len(out) > LSOF_OUTPUT_LIMIT or not out.endswith(b'\n'):
        return True
    own = ('p%d' % os.getpid()).encode('ascii')
    seen_process = False
    seen_files = set()
    awaiting_access = False
    for line in out[:-1].split(b'\n'):
        if line.startswith(b'p'):
            # Any other PID (or malformed/repeated process record) pins output.
            if line != own or seen_process:
                return True
            seen_process = True
        elif line.startswith(b'f'):
            descriptor = line[1:]
            if not seen_process or awaiting_access or not descriptor.isdigit() or len(descriptor) > 10:
                return True
            fd = int(descriptor)
            if fd > 2147483647 or str(fd).encode('ascii') != descriptor or fd in seen_files:
                return True
            seen_files.add(fd)
            awaiting_access = True
        elif line == b'ar' and awaiting_access:
            awaiting_access = False
        else:
            return True
    return not seen_files or awaiting_access


def read_lsof_output(process):
    """Bound both inspection time and memory; any stderr makes it incomplete."""
    deadline = time.monotonic() + LSOF_TIMEOUT_SECONDS
    out = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, LSOF_TIMEOUT_SECONDS)
            for key, _ in selector.select(remaining):
                block = os.read(key.fd, min(4096, LSOF_OUTPUT_LIMIT - len(out) + 1))
                if not block:
                    selector.unregister(key.fileobj)
                elif key.fileobj is process.stderr:
                    raise ValueError('Incomplete native open-file inspection')
                elif len(out) + len(block) > LSOF_OUTPUT_LIMIT:
                    raise ValueError('Native open-file inspection exceeds limit')
                else:
                    out.extend(block)
        process.wait(timeout=max(0, deadline - time.monotonic()))
    return bytes(out)


def writers_present(path):
    """Any external opener, own writer or incomplete inspection pins output."""
    if sys.platform != 'darwin':
        return True
    try:
        # Native macOS emits f fields even with -Fp. Request and validate the
        # descriptor and access fields explicitly, rather than ignoring them.
        process = subprocess.Popen(['/usr/sbin/lsof', '-nP', '-Fpfa', '--', path],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return True
    try:
        out = read_lsof_output(process)
        if process.returncode == 1 and not out:
            return False
        if process.returncode != 0:
            return True
        # No duplicate execution for this command is admitted. Only this
        # agent's read-only verification descriptors may be ignored.
        return lsof_pins_output(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return True
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        process.stdout.close()
        process.stderr.close()


def inspect_outputs(home, identifier, active):
    base = os.path.join(str(home), 'commands', hashlib.sha256(identifier.encode()).hexdigest())
    directory = output_dir(home, identifier)
    opened = []
    try:
        for name in ('stdout', 'stderr'):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                continue  # An interrupted cleanup can already have removed one.
            opened.append((name, fd))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
                raise ValueError('Unsafe maintenance diagnostic file')
        return directory, opened
    except BaseException:
        for _, fd in opened:
            os.close(fd)
        os.close(directory)
        raise


def fingerprint(fd):
    info = os.fstat(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    value = hashlib.sha256()
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        value.update(block)
    after = os.fstat(fd)
    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Maintenance diagnostic changed during verification')
    return {'dev': info.st_dev, 'ino': info.st_ino, 'bytes': info.st_size, 'sha256': value.hexdigest()}


def capture_outputs(home, identifier, result):
    directory, opened = inspect_outputs(home, identifier, lambda path: False)
    try:
        if {name for name, _ in opened} != {'stdout', 'stderr'}:
            raise ValueError('Missing diagnostic publication source')
        outputs = {}
        for name, fd in opened:
            before = fingerprint(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            text_hash = hashlib.sha256()
            with io.open(os.dup(fd), 'r', encoding='utf-8', errors='replace', newline=None) as stream:
                while True:
                    block = stream.read(65536)
                    if not block:
                        break
                    text_hash.update(block.encode('utf-8'))
            if text_hash.hexdigest() != hashlib.sha256(result[name].encode('utf-8')).hexdigest():
                raise ValueError('Published maintenance result differs from diagnostic source')
            if fingerprint(fd) != before:
                raise ValueError('Maintenance diagnostic changed during publication')
            outputs[name] = before
        return outputs
    finally:
        for _, fd in opened:
            os.close(fd)
        os.close(directory)


def payload_matches(value, result_digest, retired):
    if retired:
        return value.get('result_retired') is True and value.get('result_sha256') == result_digest
    return sha(value) == result_digest


def trim(db, home, limits=None, now=None, active=writers_present, after_unlink=None):
    limits = policy(limits)
    now = time.time() if now is None else now
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('Invalid maintenance clock')
    # Call under the agent's existing LOCK. No running/uncertain/unacknowledged
    # or pre-upgrade row is selected, even if its files appear old or large.
    rows = db.execute('SELECT r.id,r.completed,r.result_digest,r.outputs_retired,r.result_retired,r.output_manifest '
                      'FROM maintenance_output_retention r JOIN commands c ON c.id=r.id '
                      "WHERE c.state='reported' AND r.acked IS NOT NULL AND r.completed IS NOT NULL "
                      'AND (r.outputs_retired=0 OR r.result_retired=0) ORDER BY r.completed DESC,r.id DESC').fetchall()
    result_count = output_count = result_bytes = output_bytes = 0
    report = {'outputs_retired': [], 'results_retired': [], 'pinned': []}
    for identifier, completed, result_digest, outputs_retired, result_retired, output_manifest in rows:
        raw = db.execute('SELECT result FROM commands WHERE id=?', (identifier,)).fetchone()[0]
        value = json.loads(raw)
        if not payload_matches(value, result_digest, result_retired):
            report['pinned'].append(identifier)
            continue
        recent = now - completed <= limits['max_age_seconds']
        if not outputs_retired:
            directory = None
            opened = []
            try:
                directory, opened = inspect_outputs(home, identifier, active)
                expected_outputs = json.loads(output_manifest)
                size = sum(os.fstat(fd).st_size for _, fd in opened)
                keep = recent and output_count < limits['output_count'] and output_bytes + size <= limits['output_bytes']
                if keep:
                    output_count += 1
                    output_bytes += size
                else:
                    base = os.path.join(str(home), 'commands', hashlib.sha256(identifier.encode()).hexdigest())
                    if any(fingerprint(fd) != expected_outputs[name] for name, fd in opened):
                        raise ValueError('Maintenance diagnostics differ from acknowledged publication')
                    if any(active(base + '/' + name) for name, _ in opened):
                        raise ValueError('Maintenance output remains open or inspection is incomplete')
                    for name, fd in opened:
                        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        prior = os.fstat(fd)
                        if (current.st_dev, current.st_ino) != (prior.st_dev, prior.st_ino):
                            raise ValueError('Maintenance output identity changed')
                        base = os.path.join(str(home), 'commands', hashlib.sha256(identifier.encode()).hexdigest())
                        check = output_dir(home, identifier)
                        try:
                            if (os.fstat(check).st_dev, os.fstat(check).st_ino) != (os.fstat(directory).st_dev, os.fstat(directory).st_ino):
                                raise ValueError('Maintenance output directory changed')
                        finally:
                            os.close(check)
                        if active(base + '/' + name) or fingerprint(fd) != expected_outputs[name]:
                            raise ValueError('Maintenance output reopened or changed before cleanup')
                        os.unlink(name, dir_fd=directory)
                        os.fsync(directory)
                        if after_unlink is not None:
                            after_unlink(identifier, name)
                    with db:
                        db.execute('UPDATE maintenance_output_retention SET outputs_retired=1,output_manifest=NULL WHERE id=?', (identifier,))
                    report['outputs_retired'].append(identifier)
            except (OSError, ValueError):
                report['pinned'].append(identifier)
                continue
            finally:
                for _, fd in opened:
                    os.close(fd)
                if directory is not None:
                    os.close(directory)
        if not result_retired:
            size = len(raw.encode('utf-8'))
            if recent and result_count < limits['result_count'] and result_bytes + size <= limits['result_bytes']:
                result_count += 1
                result_bytes += size
            else:
                tombstone = {'state': value['state'], 'result_retired': True, 'result_sha256': result_digest}
                if 'exit_code' in value:
                    tombstone['exit_code'] = value['exit_code']
                with db:
                    db.execute('UPDATE commands SET result=? WHERE id=? AND state=\'reported\'',
                               (json.dumps(tombstone), identifier))
                    db.execute('UPDATE maintenance_output_retention SET result_retired=1 WHERE id=?', (identifier,))
                report['results_retired'].append(identifier)
    report.update(retained_output_bytes=output_bytes, retained_result_bytes=result_bytes)
    return report
