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

ROOT = Path(os.environ.get('BIOME_CODING_HOME', Path.home() / '.local/share/biome-lmstudio'))
sys.path.insert(0, os.environ.get('BIOME_GATEWAY_CODE', str(Path.home() / '.local/share/biome-mac-terminal')))
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
    if cfg.get('container'):
        import container_runner
        return container_runner.start(args)
    return host_start_command(args)

def host_start_command(args):
    cfg = config()
    root = Path(cfg['workspace']).resolve(strict=True)
    cwd = (root / args.get('directory', '.')).resolve(strict=True)
    if not cwd.is_relative_to(root) or not cwd.is_dir():
        raise ValueError('directory must be within the coding workspace')
    command = args['command']
    if not isinstance(command, str) or not command.strip():
        raise ValueError('command must be nonempty')
    tool_path=cfg.get('terminal_tool_path') or os.environ.get('BIOME_TERMINAL_TOOL_PATH') or '/opt/homebrew/bin:'+str(Path.home()/'miniconda3/bin')
    script = 'export PATH='+shlex.quote(tool_path)+':"$PATH"\ncd ' + shlex.quote(str(cwd)) + ' && ' + command
    if len(script.encode()) > 4096:
        raise ValueError('Command exceeds the terminal contract; write a script file and execute it')
    operation = 'lmc-' + uuid.uuid4().hex
    path = record_path(operation)
    state = {'operation': operation, 'script': script, 'resource': cfg['terminal_resource'], 'stage': 'acquiring'}
    save(path, state)
    return command_status({'operation': operation})

def command_status(args):
    saved = json.loads(record_path(args["operation"]).read_text())
    if saved.get("backend") == "docker":
        import container_runner
        import time
        result = container_runner.status(args["operation"])
        while result['state'] == 'running':
            time.sleep(1)
            result = container_runner.status(args["operation"])
        return result

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
    {'name': 'start_command', 'description': 'Start a command in a Docker container on available Biome capacity. The project .biome-container.json selects its image. Build output files are returned as an artifact archive. Returns a durable operation ID; call command_status until settled. Do not commit or push unless the task explicitly authorizes it.', 'inputSchema': {'type': 'object', 'properties': {'command': {'type': 'string'}, 'directory': {'type': 'string', 'description': 'Directory relative to the workspace, default .'}}, 'required': ['command'], 'additionalProperties': False}},
    {'name': 'command_status', 'description': 'Read or advance a previously started command. Inspect actual exit_code, stdout and stderr. Never report success for queued, claimed, uncertain, or nonzero exit.', 'inputSchema': {'type': 'object', 'properties': {'operation': {'type': 'string'}}, 'required': ['operation'], 'additionalProperties': False}},
]

sys.path.insert(0, os.environ.get('BIOME_GIT_TOOLS_CODE', str(Path(__file__).resolve().parent)))
from agent_tools import TOOLS as GIT_TOOLS, git_tools, git_start, git_status
TOOLS.extend(GIT_TOOLS)

sys.path.insert(0, os.environ.get('BIOME_WORKSPACE_TOOLS_CODE', str(Path(__file__).resolve().parent)))
from workspace_agent_tools import TOOLS as WS_TOOLS, workspace_tools, workspace_start, workspace_status
TOOLS.extend(WS_TOOLS)

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
            fn = {'start_command': start_command, 'command_status': command_status, 'git_tools': git_tools, 'git_start': git_start, 'git_status': git_status, 'workspace_tools': workspace_tools, 'workspace_start': workspace_start, 'workspace_status': workspace_status}[params['name']]
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