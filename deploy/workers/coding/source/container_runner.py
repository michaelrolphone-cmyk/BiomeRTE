"""Durable workspace execution on any available Biome Docker provider.

The coordinator stages bytes: Docker workers need no shared host path.
Each CLI mutation is submitted once; status reads reconcile its operation ID.
Containers run detached, so CLI request durations do not limit build duration.
"""
import base64
import fcntl
import hashlib
import io
import json
import os
import shlex
from pathlib import Path
import sys
import tarfile
import time
import uuid

ROOT = Path(os.environ.get('BIOME_CODING_HOME', Path.home() / '.local/share/biome-lmstudio'))
sys.path.insert(0, os.environ.get('BIOME_GATEWAY_CODE', str(Path.home() / '.local/share/biome-mac-terminal')))
import mac_provider as biome


def save(path, state):
    biome.save_json(path, state)


def config():
    return json.loads((ROOT / 'coding-config.json').read_text())


def path_for(operation):
    if not operation.startswith('lmc-') or not all(c.isalnum() or c == '-' for c in operation):
        raise ValueError('Invalid operation')
    return ROOT / 'commands' / (operation + '.json')


def archive_workspace(cwd, container_path=None):
    data = io.BytesIO()
    manifest = []
    with tarfile.open(fileobj=data, mode='w') as archive:
        prefix = (container_path or '/workspace').lstrip('/')
        entry = tarfile.TarInfo(prefix)
        entry.type, entry.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(entry)
        for path in sorted(cwd.rglob('*')):
            rel = path.relative_to(cwd)
            if '.biome-results' in rel.parts:
                continue
            archive.add(path, arcname=prefix + '/' + rel.as_posix(), recursive=False)
            if path.is_file() and not path.is_symlink():
                manifest.append([rel.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()])
    snapshot = hashlib.sha256(json.dumps(manifest, separators=(',', ':')).encode()).hexdigest()
    return data.getvalue(), snapshot


def start(args):
    cfg = config()
    root = Path(cfg['workspace']).resolve(strict=True)
    cwd = (root / args.get('directory', '.')).resolve(strict=True)
    if not cwd.is_relative_to(root) or not cwd.is_dir():
        raise ValueError('directory must be in the coding workspace')
    command = args['command']
    if not isinstance(command, str) or not command.strip():
        raise ValueError('command must be nonempty')
    profile = dict(cfg.get('container', {}))
    project = cwd / '.biome-container.json'
    if project.exists():
        profile.update(json.loads(project.read_text()))
    image = args.get('image') or profile.get('image')
    if not isinstance(image, str) or not image.strip():
        raise ValueError('Set the project container image in .biome-container.json')
    operation = 'lmc-' + uuid.uuid4().hex
    path = path_for(operation)
    path.parent.mkdir(parents=True, exist_ok=True)
    data, snapshot = archive_workspace(cwd, str(cwd))
    archive = path.with_suffix('.input.tar')
    archive.write_bytes(data)
    state = dict(operation=operation, backend='docker', stage='archive', image=image,
                 command=command, cwd=str(cwd), container_path=str(cwd), snapshot_id=snapshot,
                 archive_path=str(archive), chunks=[], upload_offset=0,
                 container=operation, sequence=0, pending=None)
    save(path, state)
    return status(operation)


def command_step(path, state, label, argv, allow_nonzero=False, **extra):
    pending = state.get('pending')
    if pending is None:
        state['sequence'] += 1
        operation = state['operation'] + '-' + label + '-' + str(state['sequence'])
        pending = dict(operation=operation, inputs=dict(argv=argv, **extra))
        state['pending'] = pending
        save(path, state)  # Persist before submitting any effect.
        biome.invoke('cap.invoke', dict(lease=state['lease'], **pending), operation + '-invoke')
    if 'completed_output' in pending:
        return pending['completed_output']
    try:
        result = biome.invoke('cap.status', {'operation': pending['operation']})
    except Exception as exc:
        if 'Unknown actions' not in str(exc):
            raise
        # Read-back established that submission did not create the action.
        # Reuse the original identical mutation identity, never a new command.
        biome.invoke('cap.invoke', dict(lease=state['lease'], **pending), pending['operation'] + '-invoke')
        return None
    if result['state'] not in ('succeeded', 'failed', 'cancelled'):
        return None
    output = result.get('result') or {}
    state.setdefault('cli_operations', []).append(pending['operation'])
    if result['state'] != 'succeeded' or (output.get('exit_code') != 0 and not allow_nonzero):
        raise RuntimeError('Docker CLI ' + label + ' failed: ' + json.dumps(output)[:2400])
    pending['completed_output'] = output
    save(path, state)
    return output


def stream_bytes(stream):
    if stream.get('truncated') or stream.get('preview_truncated') and not stream.get('chunks'):
        raise RuntimeError('Docker output artifact is truncated')
    data = bytearray()
    for chunk in stream.get('chunks', []):
        result = biome.invoke('artifact.get', {'sha256': chunk['sha256']})
        block = base64.b64decode(result['data'])
        if hashlib.sha256(block).hexdigest() != chunk['sha256']:
            raise RuntimeError('Artifact digest mismatch')
        data.extend(block)
    if 'total_bytes' in stream and len(data) != stream['total_bytes']:
        raise RuntimeError('Incomplete Docker output artifact')
    return bytes(data)


def status(operation):
    path = path_for(operation)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        if state['stage'] in ('done', 'unavailable'):
            return public(state)
        if state['stage'] == 'archive':
            with open(state['archive_path'], 'rb') as source:
                source.seek(state['upload_offset'])
                block = source.read(256 * 1024)
            if block:
                result = biome.invoke('artifact.put', {'data': base64.b64encode(block).decode()},
                                      operation + '-archive-' + str(state['upload_offset']))
                digest = result.get('sha256') or result.get('id')
                if not digest:
                    raise RuntimeError('Artifact upload returned no identity')
                state['chunks'].append(digest)
                state['upload_offset'] += len(block)
            else:
                state['stage'] = 'acquiring'
            save(path, state)
        if state['stage'] == 'acquiring':
            # No provider, host, or resource preference: the Biome pool selects capacity.
            acquisition = {'contract': 'docker.cli:1', 'task': operation + '-worker-' + str(len(state.get('attempted_resources', [])))}
            if state.get('next_resource'):
                acquisition['resource'] = state['next_resource']
            lease = biome.invoke('cap.acquire', acquisition, operation + '-acquire-' + str(len(state.get('attempted_resources', []))))
            state.update(lease=lease['id'], stage='binding')
            save(path, state)
        if state['stage'] == 'binding':
            lease = biome.invoke('cap.lease', {'lease': state['lease']})
            if lease['state'] != 'bound':
                return public(state)
            state.update(resource=lease['resource'], provider=lease['provider'], stage='create')
            save(path, state)
        container_path = state.get('container_path', '/workspace')
        container_command = state['command']
        if 'container_path' not in state and state['cwd'] != '/workspace':
            # Compatibility for jobs staged by the prior /workspace version.
            container_command = ('mkdir -p ' + shlex.quote(str(Path(state['cwd']).parent))
                                 + ' && ln -s /workspace ' + shlex.quote(state['cwd'])
                                 + ' && ' + container_command)
        steps = {
            'create': ('create', ['create', '--name', state['container'], '--label', 'biome.operation=' + operation,
                                   '--workdir', container_path, state['image'], '/bin/sh', '-lc', container_command], {}, 'stage'),
            'reconcile_create': ('reconcile-create', ['inspect', state['container']], {}, 'acquiring'),
            'stage': ('stage', ['cp', '-', state['container'] + ':/'], {'stdin_chunks': state['chunks']}, 'start'),
            'start': ('start', ['start', state['container']], {}, 'inspect'),
            'inspect': ('inspect', ['inspect', '--format', '{{json .State}}', state['container']], {}, 'logs'),
            'logs': ('logs', ['logs', state['container']], {}, 'outputs'),
            'outputs': ('outputs', ['cp', state['container'] + ':' + container_path + '/.', '-'], {}, 'cleanup'),
            'cleanup': ('cleanup', ['rm', state['container']], {}, 'release'),
        }
        stage = state['stage']
        if stage in steps:
            label, argv, extra, following = steps[stage]
            output = command_step(path, state, label, argv, allow_nonzero=stage in ('create', 'reconcile_create'), **extra)
            if output is None:
                return public(state)
            if stage == 'create' and output['exit_code'] != 0:
                state['create_error'] = output
                following = 'reconcile_create'
            elif stage == 'reconcile_create':
                if output['exit_code'] == 0:
                    # The named container exists: reconcile it on this worker.
                    following = 'stage'
                elif 'No such object' in output.get('stderr', '') or 'No such container' in output.get('stderr', ''):
                    attempts = state.setdefault('attempted_resources', [])
                    attempts.append(state['resource'])
                    state.setdefault('worker_failures', []).append(dict(resource=state['resource'], error=state['create_error']))
                    # No container exists; transfer is now safe, with no work replay.
                    biome.invoke('cap.release', {'lease': state['lease']}, operation + '-release-unusable-' + str(len(attempts)))
                    offers = biome.invoke('cap.discover', {'contract': 'docker.cli:1'})['offers']
                    candidates = [offer for offer in offers if offer['state'] == 'ready' and offer['resource'] not in attempts]
                    if candidates:
                        state['next_resource'] = candidates[0]['resource']
                        following = 'acquiring'
                    else:
                        following = 'unavailable'
                else:
                    raise RuntimeError('Cannot establish container absence before provider change: ' + json.dumps(output))
            elif stage == 'inspect':
                inspection = json.loads(output['stdout'])
                state['container_state'] = inspection
                if inspection.get('Running'):
                    state['pending'] = None
                    save(path, state)
                    return public(state)
                state['exit_code'] = inspection['ExitCode']
            elif stage == 'logs':
                state['logs'] = output
            elif stage == 'outputs':
                stream = output['streams']['stdout']
                data = stream_bytes(stream)
                archive = path.with_suffix('.output.tar')
                archive.write_bytes(data)
                state['output_artifact'] = dict(sha256=hashlib.sha256(data).hexdigest(),
                                               size=len(data), chunks=stream['chunks'], local_path=str(archive))
            state['stage'] = following
            state['pending'] = None
            save(path, state)
        if state['stage'] == 'release':
            biome.invoke('cap.release', {'lease': state['lease']}, operation + '-release')
            state['stage'] = 'done'
            save(path, state)
        return public(state)


def public(state):
    done = state['stage'] == 'done'
    result = None
    if done:
        logs = state['logs']
        result = dict(exit_code=state['exit_code'], stdout=logs.get('stdout', ''), stderr=logs.get('stderr', ''),
                      streams=logs.get('streams', {}), execution='docker', image=state['image'],
                      resource=state['resource'], provider=state['provider'], container=state['container'],
                      container_state=state['container_state'], snapshot_id=state['snapshot_id'],
                      output_artifact=state['output_artifact'], cli_operations=state['cli_operations'],
                      worker_failures=state.get('worker_failures', []),
                      recovery_evidence=state.get('recovery_evidence', []))
    if state['stage'] == 'unavailable':
        result = dict(exit_code=None, infrastructure_error='No discovered Docker worker could create this image', worker_failures=state['worker_failures'])
    return dict(operation=state['operation'], state='succeeded' if done else ('failed' if state['stage'] == 'unavailable' else 'running'),
                stage=state['stage'], result=result,
                next='Call command_status until settled; inspect actual container exit_code.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    parser.add_argument('--command', required=True)
    parser.add_argument('--image')
    args = parser.parse_args()
    response = start(vars(args))
    while response['state'] == 'running':
        time.sleep(1)
        response = status(response['operation'])
    print(json.dumps(response), flush=True)
    exit_code = response['result'].get('exit_code')
    raise SystemExit(exit_code if isinstance(exit_code, int) else 125)
