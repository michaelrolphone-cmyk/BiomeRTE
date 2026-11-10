"""One noninteractive Docker CLI passthrough for BiomeRTE (Python 3.4+)."""
import base64
import binascii
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time

# Both CLI providers return the same lossless BiomeRTE artifact stream format.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pi-terminal'))
from terminal import store_stream
sys.path.pop(0)

NAME = 'docker.cli'
CONTRACT = NAME + ':1'
INPUTS = {'argv': 'list', 'cwd': 'str?', 'stdin': 'str?', 'stdin_base64': 'str?',
          'stdin_chunks': 'list?', 'timeout_seconds': 'int?'}
OUTPUTS = {'executed': 'bool', 'exit_code': 'int', 'timed_out': 'bool', 'stdout': 'str',
           'stderr': 'str', 'streams': 'obj'}


def usage():
    return {
        'before': 'Use this trusted-operator capability for the Docker CLI on the provider host. Docker socket access '
                  'allows host administration. Use a separate lease for each parallel command.',
        'parameters': {
            'argv': 'Pass every argument after docker as a separate string, including global flags, subcommands and their flags. '
                    'For docker ps -a use ["ps","-a"]. For docker exec, include the container and its argument vector. Shell syntax is literal.',
            'cwd': 'Optional absolute directory on the provider host, for example a build context. Relative CLI paths resolve there; default is the provider workspace.',
            'stdin': 'Optional UTF-8 input, for example a Dockerfile with argv ["build","-"]. Input closes at EOF.',
            'stdin_base64': 'Optional base64 bytes for binary stdin, for example a tar stream with ["load"]. Choose only one stdin field.',
            'stdin_chunks': 'Optional ordered artifact chunk SHA-256 strings or {sha256,size} entries, as returned by streams.stdout.chunks. '
                            'Chunks are verified and concatenated as stdin; limit 128 MiB. Use host paths for larger inputs.',
            'timeout_seconds': 'Optional command timeout, 1–3600 seconds; default 600. For long-lived containers use run -d. '
                               'This is a completed-command interface with finite stdin; live interactive TTY attachment is unavailable.'},
        'example_inputs': {'argv': ['version', '--format', '{{.Server.Version}}']},
        'read_result': 'Read cap.status.result.exit_code and timed_out. stdout and stderr preview the first 4096 bytes. '
                       'For exact captured bytes, fetch each ordered stream chunk with artifact.get and base64-decode it. '
                       'Each stream retains up to 16 MiB; total_bytes and truncated report overflow. For large archives use docker save -o on the host.',
        'errors': 'Docker supplies its own exit code and error text, including unsupported flags for the installed version. '
                  'executed=false means the CLI did not start. A timeout kills the local CLI process; inspect daemon state before retrying a mutation.'
    }


def read_json(path):
    with open(path) as stream:
        return json.load(stream)


def rejected(message, code=126):
    return {'executed': False, 'exit_code': code, 'timed_out': False, 'stdout': '',
            'stderr': str(message), 'streams': {}}


def decode64(value):
    try:
        return base64.b64decode(value.encode('ascii'), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid base64 stdin')


def prepare_input(args, target, remote):
    fields = [k for k in ('stdin', 'stdin_base64', 'stdin_chunks') if k in args]
    if len(fields) > 1:
        raise ValueError('Choose one stdin field')
    if 'stdin_chunks' in args:
        for ref in args['stdin_chunks']:
            sha = ref.get('sha256') if isinstance(ref, dict) else ref
            if not isinstance(sha, str) or not re.match(r'^[0-9a-f]{64}\Z', sha):
                raise ValueError('Invalid artifact digest')
            artifact = remote.call('artifact.get', {'sha256': sha})
            block = decode64(artifact['data'])
            if hashlib.sha256(block).hexdigest() != sha or (
                    isinstance(ref, dict) and ref.get('size', len(block)) != len(block)):
                raise ValueError('Input artifact integrity check failed')
            if target.tell() + len(block) > 128 * 1024 * 1024:
                raise ValueError('stdin exceeds 128 MiB')
            target.write(block)
    elif fields:
        data = decode64(args['stdin_base64']) if 'stdin_base64' in args else args['stdin'].encode('utf-8')
        if len(data) > 128 * 1024 * 1024:
            raise ValueError('stdin exceeds 128 MiB')
        target.write(data)
    target.seek(0)


def capture(proc, directory, deadline, limit, progress_path=None):
    from biomerte.command_watch import OutputProgress
    progress = OutputProgress(progress_path)
    paths = {}
    counts = {'stdout': 0, 'stderr': 0}
    timed_out = False
    stopped = False
    try:
        with selectors.DefaultSelector() as sel, open(os.path.join(directory, 'out'), 'wb') as out, \
                open(os.path.join(directory, 'err'), 'wb') as err:
            for name, pipe, dest in [('stdout', proc.stdout, out), ('stderr', proc.stderr, err)]:
                paths[name] = dest.name
                sel.register(pipe, selectors.EVENT_READ, (name, dest))
            while sel.get_map():
                progress.report(paths)
                if progress.stopped():
                    stopped = True
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    timed_out = True
                    break
                for key, _ in sel.select(min(left, 1)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        sel.unregister(key.fileobj)
                        continue
                    name, dest = key.data
                    remaining = max(0, limit - counts[name])
                    dest.write(data[:remaining])
                    counts[name] += len(data)
            if not timed_out and not stopped:
                try:
                    proc.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
    finally:
        if timed_out or proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()
    return paths, counts, timed_out


def execute(cfg, args, remote, progress_path=None):
    argv = args['argv']
    if not isinstance(argv, list) or any(not isinstance(a, str) or '\0' in a for a in argv):
        return rejected('argv must be a list of strings without NUL')
    cwd = args.get('cwd', cfg['workspace'])
    if '\0' in cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return rejected('cwd must be an existing absolute directory on the provider host')
    timeout = args.get('timeout_seconds', 600)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        return rejected('timeout_seconds must be between 1 and 3600')
    with tempfile.TemporaryDirectory(dir=cfg['spool']) as directory, tempfile.TemporaryFile(dir=directory) as inp:
        try:
            prepare_input(args, inp, remote)
            proc = subprocess.Popen([cfg['executable']] + argv, cwd=cwd, stdin=inp, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True,
                                    env=dict(os.environ, DOCKER_HOST='unix://' + cfg['socket'],
                                             DOCKER_CONFIG=cfg['client_config']))
        except (ValueError, OSError) as exc:
            return rejected(exc)
        paths, counts, expired = capture(proc, directory, time.monotonic() + timeout, 16 * 1024 * 1024, progress_path)
        result = {'executed': True, 'exit_code': proc.returncode, 'timed_out': expired, 'streams': {},
                  'evidence': {'quiescent': True, 'cli_pid': proc.pid, 'exit_code': proc.returncode,
                               'reason': 'stop_requested' if progress_path and os.path.exists(progress_path+'.stop') else 'exited'}}
        if progress_path:
            # Persist local completion before delivery; recovery never invokes CLI.
            import shutil
            from biomerte.command_runner import save
            from biomerte.store import digest
            retained={}
            for name,path in paths.items():
                target=os.path.join(os.path.dirname(progress_path),'docker.'+name)
                with open(path,'rb') as source, open(target,'wb') as destination:
                    shutil.copyfileobj(source,destination);destination.flush();os.fsync(destination.fileno())
                with open(target,'rb') as stream:
                    sha=hashlib.sha256(stream.read()).hexdigest()
                retained[name]={'sha256':sha,'total_bytes':counts[name]}
            save(os.path.join(os.path.dirname(progress_path),'docker-completion.json'),
                 {'input_digest':digest(args),'result':result,'streams':retained})
        result.pop('evidence',None)  # Custody proof stays in the private receipt, outside the public contract.
        for name, path in paths.items():
            result[name], meta = store_stream(path, remote, cfg.get('chunk_bytes', 256 * 1024))
            meta.update(total_bytes=counts[name], truncated=counts[name] > meta['size'])
            result['streams'][name] = meta
        return result


if __name__ == '__main__':
    from biomerte.worker import Remote
    manifest = os.path.abspath(sys.argv[1])
    cfg = read_json(manifest)
    home = os.path.dirname(manifest)
    node = read_json(os.path.join(home, 'node.json'))
    remote = Remote(cfg['url'], cfg['site'], node['node'], os.path.join(home, 'node.key'))
    if sys.argv[2:] == ['--offline']:
        offer = cfg['offers'][0]
        remote.call('cap.offer', {'provider': node['node'], 'contract': CONTRACT, 'resource': offer['resource'],
                                  'service': offer['service'], 'state': 'offline', 'capacity': offer['capacity']})
    else:
        request = json.load(sys.stdin)
        json.dump(execute(cfg, request['inputs'], remote, request.get('progress_path')), sys.stdout)
