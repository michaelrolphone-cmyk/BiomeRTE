#!/usr/bin/env python3
"""Small stdio MCP facade over BiomeRTE's durable Mac terminal capability.

Commands use the Mac user's permissions; cwd validation is not a shell sandbox.
No inference or command deadline is imposed here. Biome retains execution evidence.
"""
import json
import os
import shlex
import sys
import uuid
from pathlib import Path

ROOT = Path.home() / '.local/share/biome-lmstudio'
sys.path.insert(0, str(Path.home() / '.local/share/biome-mac-terminal'))
import mac_provider as biome

def config():
    return json.loads((ROOT / 'coding-config.json').read_text())

def record_path(operation):
    if not isinstance(operation, str) or not operation.startswith('lmc-') or not all(c.isalnum() or c == '-' for c in operation):
        raise ValueError('Invalid command operation ID')
    return ROOT / 'commands' / (operation + '.json')

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    biome.save_json(path, data)

def start_command(args):
    cfg = config()
    root = Path(cfg['workspace']).resolve(strict=True)
    cwd = (root / args.get('directory', '.')).resolve(strict=True)
    if not cwd.is_relative_to(root) or not cwd.is_dir():
        raise ValueError('directory must be within the coding workspace')
    command = args['command']
    if not isinstance(command, str) or not command.strip():
        raise ValueError('command must be nonempty')
    tool_path='/opt/homebrew/bin:'+str(Path.home()/'miniconda3/bin')
    script = 'export PATH='+shlex.quote(tool_path)+':"$PATH"\ncd ' + shlex.quote(str(cwd)) + ' && ' + command
    if len(script.encode()) > 4096:
        raise ValueError('Command exceeds the terminal contract; write a script file and execute it')
    operation = 'lmc-' + uuid.uuid4().hex
    path = record_path(operation)
    state = {'operation': operation, 'script': script, 'resource': cfg['terminal_resource'], 'stage': 'acquiring'}
    save(path, state)
    return command_status({'operation': operation})

def command_status(args):
    operation = args['operation']
    path = record_path(operation)
    state = json.loads(path.read_text())
    if state['stage'] == 'acquiring':
        lease = biome.invoke('cap.acquire', {'contract': 'host.terminal.run:1', 'resource': state['resource'], 'task': operation}, operation + '-acquire')
        state.update(lease=lease['id'], stage='binding')
        save(path, state)
    if state['stage'] == 'binding':
        lease = biome.invoke('cap.lease', {'lease': state['lease']})
        if lease['state'] != 'bound':
            return {'operation': operation, 'state': 'waiting_for_terminal', 'lease_state': lease['state']}
        biome.invoke('cap.invoke', {'lease': state['lease'], 'operation': operation, 'inputs': {'argv': ['/bin/bash', '-c', state['script']]}}, operation + '-invoke')
        state['stage'] = 'submitted'
        save(path, state)
    result = biome.invoke('cap.status', {'operation': operation})
    if result.get('state') in ('succeeded', 'failed', 'cancelled') and result.get('result', {}).get('evidence', {}).get('quiescent') is True:
        if state['stage'] != 'released':
            biome.invoke('cap.release', {'lease': state['lease']}, operation + '-release')
            state['stage'] = 'released'
            save(path, state)
    return {'operation': operation, 'state': result['state'], 'result': result.get('result'), 'next': 'Call command_status with this operation until settled. A successful execution must have exit_code 0; queued or claimed is not completion.'}

TOOLS = [
    {'name': 'start_command', 'description': 'Start a shell command in the Mac coding workspace using BiomeRTE. Use for git inspection, builds and tests. Returns a durable operation ID; call command_status until settled. Do not commit or push unless the task explicitly authorizes it.', 'inputSchema': {'type': 'object', 'properties': {'command': {'type': 'string'}, 'directory': {'type': 'string', 'description': 'Directory relative to the workspace, default .'}}, 'required': ['command'], 'additionalProperties': False}},
    {'name': 'command_status', 'description': 'Read or advance a previously started command. Inspect actual exit_code, stdout and stderr. Never report success for queued, claimed, uncertain, or nonzero exit.', 'inputSchema': {'type': 'object', 'properties': {'operation': {'type': 'string'}}, 'required': ['operation'], 'additionalProperties': False}},
]

def dispatch(message):
    method = message['method']
    if method == 'initialize':
        return {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'biome-coding-terminal', 'version': '1.0.0'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        params = message['params']
        try:
            fn = {'start_command': start_command, 'command_status': command_status}[params['name']]
            result = fn(params.get('arguments', {}))
            return {'content': [{'type': 'text', 'text': json.dumps(result)}]}
        except Exception as exc:
            return {'isError': True, 'content': [{'type': 'text', 'text': str(exc)[:1200]}]}
    raise ValueError('Unsupported MCP method: ' + method)

def main():
    os.umask(0o077)
    for line in sys.stdin:
        message = json.loads(line)
        if 'id' not in message:
            continue
        try:
            response = {'result': dispatch(message)}
        except Exception as exc:
            response = {'error': {'code': -32601, 'message': str(exc)}}
        print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], **response}), flush=True)

if __name__ == '__main__':
    main()
