"""Commission and install a sibling terminal worker on the controller's Pi."""
import argparse
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile

from terminal import CONTRACT, DEFAULT_SLOTS, OUTPUTS, check_argv, read_json, terminal_pool


def replace(path, data):
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False) as temp:
        try:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            temp.close()
            os.replace(temp.name, path)
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)


def quote(value):
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('Invalid service argument')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def main():
    account = pwd.getpwuid(os.getuid())
    if not account.pw_uid:
        raise SystemExit('Run this as the Pi account that owns the controller and terminal worker.')
    base = account.pw_dir
    here = os.path.dirname(os.path.realpath(__file__))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', default=os.path.join(here, '..', '..'))
    parser.add_argument('--controller-home', default=os.environ.get(
        'BIOME_HOME', os.path.join(base, '.local/share/biomerte')))
    parser.add_argument('--slots', type=int, default=DEFAULT_SLOTS,
                        help='maximum simultaneous BiomeRTE terminal sessions (default 16)')
    parser.add_argument('--port', type=int, help='override the controller loopback port')
    args = parser.parse_args()
    if args.slots < 1:
        parser.error('--slots must be positive')
    checkout = os.path.realpath(args.checkout)
    sys.path.insert(0, checkout)
    from biomerte.app import initialize
    from biomerte.worker import Remote

    home = os.path.join(base, '.local/share/biomerte-terminal')
    runtime = os.path.join(base, '.local/share/capabilities-terminal')
    sys.path.insert(0, runtime)
    from supervisor import execute  # Verify the installed supervisor is importable.
    from sdk import Journal
    config = read_json(os.path.join(base, '.config/capabilities-terminal/agent.json'))
    source = Journal(config['journal'])
    try:
        profiles = source.get('active_profiles', config['providers'])
    finally:
        source.db.close()
    profile, = [p for p in profiles if p['provider_id'] == 'linux.bash-terminal']
    check_argv([profile['executable']], profile)
    # Carry the operator's enrolled Pi memory limit into Biome; other providers
    # select their own policy.
    limits = {}
    if 'memory_bytes' in profile:
        memory = profile['memory_bytes']
        if type(memory) is not int or memory <= 0:
            raise ValueError('Expected a positive Pi memory limit')
        limits['memory_bytes'] = memory
    scoped_profile = {'executable': profile['executable'],
                      'executable_sha256': profile['executable_sha256'], 'limits': limits}

    os.umask(0o077)
    node = read_json(os.path.join(args.controller_home, 'node.json'))
    url = 'http://127.0.0.1:%d' % (args.port or node['port'])
    controller = Remote(url, node['site'], node['node'], None, token=node['admin_token'])
    provider = initialize(home)
    resource = 'terminal.' + provider['node']
    # Only the local installer uses the controller's operator identity.
    controller.call('cap.define', {'name': CONTRACT.rsplit(':', 1)[0], 'version': 1,
                    'description': 'Run the Pi terminal worker\'s digest-pinned Bash argv.',
                    'inputs': {'argv': 'list'}, 'outputs': OUTPUTS,
                    'usage': {'example_inputs': {'argv': ['/bin/bash', '-c', 'hostname']},
                              'before': 'Use the executable specified by the selected terminal offer.',
                              'parameters': {'argv': 'Pass the argument vector accepted by the selected terminal offer.'},
                              'read_result': 'Check result.exit_code and read result.stdout and result.stderr.'}})
    controller.call('peer.trust', {'public_key': provider['public_key'],
                    'label': socket.gethostname() + ' terminal',
                    'scopes': ['cap.offer', 'cap.fenced', 'provider.poll', 'provider.claim',
                               'provider.result', 'provider.custody', 'cap.heartbeat', 'cap.command.state', 'agent.progress', 'cap.status', 'artifact.put', 'cap.command.state', 'agent.progress', 'cap.heartbeat',
                               'offer:' + CONTRACT, 'resource:' + resource]})

    manifest = os.path.join(home, 'manifest.json')
    adapter = os.path.join(home, 'terminal.py')
    for name in ('terminal.py', 'terminal_limits.py', 'terminal_supervisor.py'):
        with open(os.path.join(here, name), 'rb') as src:
            replace(os.path.join(home, name), src.read())
    cfg = terminal_pool(resource, scoped_profile, [sys.executable, adapter, manifest], args.slots)
    cfg.update(url=url, site=node['site'], supervisor_dir=runtime,
               timeout=None, terminal_profile=scoped_profile)
    replace(manifest, json.dumps(cfg, indent=2).encode())
    unit = 'biomerte-terminal.service'
    # Jessie systemd needs the first ExecStart token to be an unquoted absolute path.
    cmd = [os.path.join(checkout, 'biome'), '--home', home, 'worker', manifest]
    body = ['[Unit]', 'Description=BiomeRTE Pi terminal provider', 'After=biomerte.service',
            'Wants=biomerte.service', '[Service]', 'User=' + account.pw_name, 'UMask=0077',
            'ExecStart=/usr/bin/env ' + ' '.join(quote(s) for s in cmd),
            'Restart=always', 'RestartSec=3', '[Install]', 'WantedBy=multi-user.target', '']
    path = os.path.join(home, unit)
    replace(path, '\n'.join(body).encode())
    subprocess.check_call(['sudo', 'install', '-m', '0644', path, '/etc/systemd/system/' + unit])
    for command in (['daemon-reload'], ['enable', unit], ['restart', unit], ['is-active', unit]):
        subprocess.check_call(['sudo', 'systemctl'] + command)
    print('Installed %s offering %s on %s' % (unit, CONTRACT, resource))


if __name__ == '__main__':
    main()
