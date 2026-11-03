"""Install the external Docker CLI provider on a systemd host."""
import argparse
import grp
import json
import os
import pwd
import re
import stat
import subprocess
import sys

from docker_cli import CONTRACT, NAME, INPUTS, OUTPUTS, usage

# The terminal installer already provides atomic file installation and unit quoting.
# It is imported from its directory and makes no changes when imported.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pi-terminal'))
from install import quote, replace
sys.path.pop(0)


def main():
    user = pwd.getpwuid(os.getuid())
    if not user.pw_uid:
        raise SystemExit('Run as the non-root controller owner.')
    here = os.path.dirname(os.path.realpath(__file__))
    checkout = os.path.realpath(os.path.join(here, '..', '..'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-home', default=os.path.join(user.pw_dir, '.local/share/biomerte'))
    parser.add_argument('--executable', default='/usr/local/bin/docker')
    parser.add_argument('--socket', default='/var/run/docker.sock')
    parser.add_argument('--daemon-unit', default='docker.service')
    parser.add_argument('--slots', type=int, default=4)
    args = parser.parse_args()
    if args.slots < 1 or not re.match(r'^[A-Za-z0-9_.@-]+\.service\Z', args.daemon_unit):
        parser.error('Expected positive slots and a systemd service unit')
    exe = os.path.realpath(args.executable)
    if not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        parser.error('Docker executable missing')
    sock = os.path.realpath(args.socket)
    permissions = os.stat(sock)
    if not stat.S_ISSOCK(permissions.st_mode) or not permissions.st_gid or permissions.st_mode & 0o060 != 0o060:
        parser.error('Expected a group-readable/writable Docker socket with a non-root group')
    group = grp.getgrgid(permissions.st_gid).gr_name
    os.umask(0o077)
    sys.path.insert(0, checkout)
    from biomerte.app import initialize, read_json
    from biomerte.worker import Remote
    owner = read_json(os.path.join(args.controller_home, 'node.json'))
    url = 'http://127.0.0.1:%d' % owner['port']
    admin = Remote(url, owner['site'], owner['node'], None, token=owner['admin_token'])
    home = os.path.join(user.pw_dir, '.local/share/biomerte-docker')
    node = initialize(home)
    resource = 'docker.' + node['node']
    guide = usage()
    admin.call('cap.define', {'name': NAME, 'version': 1, 'inputs': INPUTS, 'outputs': OUTPUTS,
                              'description': 'Invoke the installed Docker CLI using its native arguments and standard streams.',
                              'usage': guide})
    admin.call('peer.trust', {'public_key': node['public_key'], 'label': os.uname()[1] + ' Docker CLI',
                              'scopes': ['cap.offer', 'cap.fenced', 'provider.poll', 'provider.claim', 'provider.result',
                                         'cap.status', 'artifact.get', 'artifact.put', 'offer:' + CONTRACT, 'resource:' + resource]})
    manifest = os.path.join(home, 'manifest.json')
    adapter = os.path.join(here, 'docker_cli.py')
    offer = {'contract': CONTRACT, 'resource': resource, 'service': 'docker-cli', 'capacity': args.slots,
             'argv': [sys.executable, adapter, manifest]}
    cfg = {'url': url, 'site': owner['site'], 'offers': [offer], 'executable': exe, 'socket': sock,
           'parallelism': args.slots, 'timeout': 5400, 'poll_seconds': 1}
    for name in ('spool', 'client_config', 'workspace'):
        cfg[name] = os.path.join(home, name)
        os.makedirs(cfg[name], mode=0o700, exist_ok=True)
    replace(manifest, json.dumps(cfg, indent=2).encode())
    unit = 'biomerte-docker.service'
    boot = [os.path.join(checkout, 'biome'), '--home', home, 'worker', manifest]
    body = ['[Unit]', 'Description=BiomeRTE Docker CLI provider', 'Requires=' + args.daemon_unit,
            'Wants=biomerte.service', 'After=biomerte.service ' + args.daemon_unit, '[Service]',
            'User=' + user.pw_name, 'SupplementaryGroups=' + group, 'UMask=0077',
            'ExecStartPre=/usr/bin/env ' + ' '.join(quote(x) for x in [exe, '-H', 'unix://' + sock, 'version']),
            'ExecStart=/usr/bin/env ' + ' '.join(quote(x) for x in boot),
            'Environment=' + quote('PYTHONPATH=' + checkout),
            'ExecStopPost=-/usr/bin/env ' + ' '.join(quote(x) for x in [sys.executable, adapter, manifest, '--offline']),
            'Restart=on-failure', 'RestartSec=4', '[Install]', 'WantedBy=multi-user.target ' + args.daemon_unit, '']
    path = os.path.join(home, unit)
    replace(path, '\n'.join(body).encode())
    subprocess.check_call(['sudo', 'install', '-m', '0644', path, '/etc/systemd/system/' + unit])
    for action in (['daemon-reload'], ['enable', unit], ['restart', unit], ['is-active', unit]):
        subprocess.check_call(['sudo', 'systemctl'] + action)
    print('Installed %s with capacity %d on %s' % (CONTRACT, args.slots, resource))


if __name__ == '__main__':
    main()
