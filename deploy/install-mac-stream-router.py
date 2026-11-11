#!/usr/bin/env python3
"""Install the persistent stream owner beside the native Mac controller."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import secrets
import subprocess
import tempfile
from urllib.parse import urlsplit


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(source, python, controller, public_url):
    url = urlsplit(public_url)
    if (url.scheme != 'wss' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path != '/api/stream'):
        raise ValueError('Expected the public gateway WSS /api/stream URL')
    source = Path(source).expanduser().resolve()
    controller = Path(controller).expanduser().resolve()
    # Preserve the virtual environment interpreter path rather than resolving
    # its symlink to the base interpreter and losing its installed transports.
    python = Path(python).expanduser().absolute()
    if not (source / 'biomerte/stream_router.py').is_file() or not python.is_file():
        raise ValueError('Qualified router source and native Python are required')
    subprocess.run([str(python), '-c', 'import websockets.asyncio.server'], check=True)
    node_path = controller / 'node.json'
    node = json.loads(node_path.read_text())
    if not isinstance(node.get('stream_router_secret'), str) or len(node['stream_router_secret']) < 32:
        node['stream_router_secret'] = secrets.token_hex(32)
    node['stream_gateway_url'] = public_url
    atomic(node_path, (json.dumps(node, sort_keys=True) + '\n').encode())

    state = Path.home() / '.local/share/biomerte-stream-router'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    config = state / 'router.json'
    atomic(config, (json.dumps({'controllers': [str(controller)], 'listen': '127.0.0.1',
                              'port': 8751, 'transport': 'private_http'}, sort_keys=True) + '\n').encode())
    label = 'org.biomerte.stream-router'
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    definition = {
        'Label': label,
        'ProgramArguments': [str(python), '-m', 'biomerte.stream_router', str(config)],
        'WorkingDirectory': str(source),
        'EnvironmentVariables': {'HOME': str(Path.home()), 'PYTHONPATH': str(source),
                                 'PYTHONUNBUFFERED': '1'},
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
        'StandardOutPath': str(state / 'router.log'),
        'StandardErrorPath': str(state / 'router-error.log'),
    }
    atomic(plist, plistlib.dumps(definition))
    domain = 'gui/' + str(os.getuid())
    existing = subprocess.run(['launchctl', 'print', domain + '/' + label],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if existing.returncode == 0:
        subprocess.run(['launchctl', 'bootout', domain + '/' + label], check=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], check=True)
    print(json.dumps({'service': label, 'listener': 'http://127.0.0.1:8751',
                      'public_stream_url': public_url, 'authority': node['node'],
                      'controller_restart_required': True,
                      'private_gateway_binding_required': 'CUSTOMER_HTTP_STREAM_ROUTER'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--controller', required=True)
    parser.add_argument('--public-url', required=True)
    install(**vars(parser.parse_args()))
