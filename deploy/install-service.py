#!/usr/bin/env python3
"""Install a user service for this checkout on macOS or Linux."""
import os
import platform
import plistlib
import subprocess
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
python = os.path.realpath(sys.executable)
data = os.path.abspath(os.environ.get('BIOME_HOME', os.path.expanduser('~/.local/share/biomerte')))
subprocess.check_call([python, '-m', 'biomerte', '--home', data, 'init'], cwd=root)
args = [python, '-m', 'biomerte', '--home', data, 'serve']
if platform.system() == 'Darwin':
    dest = os.path.expanduser('~/Library/LaunchAgents')
    os.makedirs(dest, exist_ok=True)
    name = 'org.biomerte.controller'
    path = os.path.join(dest, name + '.plist')
    d = {'Label': name, 'ProgramArguments': args, 'WorkingDirectory': root, 'RunAtLoad': True,
         'KeepAlive': True, 'StandardOutPath': os.path.join(data, 'service.log'),
         'StandardErrorPath': os.path.join(data, 'service-error.log')}
    with open(path, 'wb') as f:
        plistlib.dump(d, f)
    dom = 'gui/' + str(os.getuid())
    subprocess.call(['launchctl', 'bootout', dom + '/' + name], stderr=subprocess.DEVNULL)
    subprocess.check_call(['launchctl', 'bootstrap', dom, path])
elif platform.system() == 'Linux':
    # The system option supports older Pi installations without a user systemd manager.
    system = '--system' in sys.argv
    dest = '/etc/systemd/system' if system else os.path.expanduser('~/.config/systemd/user')
    contents = ['[Unit]', 'Description=BiomeRTE controller', 'After=network.target', '[Service]']
    if system:
        import getpass
        contents.append('User=' + getpass.getuser())

    def quote(v):
        if '\n' in v or '\r' in v:
            raise ValueError('Invalid service path')
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
    contents += ['WorkingDirectory=' + quote(root), 'ExecStart=' + ' '.join(quote(a) for a in args),
                 'Restart=on-failure', 'RestartSec=3', 'UMask=0077', '[Install]',
                 'WantedBy=' + ('multi-user.target' if system else 'default.target')]
    unit = 'biomerte.service'
    body = ('\n'.join(contents) + '\n').encode()
    if system:
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            f.write(body)
            f.flush()
            subprocess.check_call(['sudo', 'install', '-m', '0644',
                                  f.name, os.path.join(dest, unit)])
        ctl = ['sudo', 'systemctl']
    else:
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, unit), 'wb') as f:
            f.write(body)
        ctl = ['systemctl', '--user']
    subprocess.check_call(ctl + ['daemon-reload'])
    subprocess.check_call(ctl + ['enable', unit])
    subprocess.check_call(ctl + ['restart', unit])
else:
    raise SystemExit('Use python3 -m biomerte serve on this platform.')
