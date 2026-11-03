"""Commission the standalone BiomeRTE file provider on a systemd Raspberry Pi."""
import argparse
import json
import os
import pwd
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from storage import OPERATIONS, OUTPUTS, SCHEMA


def write_atomic(path, body):
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False) as tmp:
        try:
            tmp.write(body)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)


def service_arg(value):
    if any(c in value for c in '\0\r\n'):
        raise ValueError('Invalid systemd argument')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def open_volume(root, user, previous):
    if not os.path.ismount(root):
        raise ValueError('Storage path must already be mounted: ' + root)
    directory = os.path.join(root, 'biomerte-files')
    path = os.path.join(directory, 'files.sqlite')
    if previous and not os.path.isfile(path):
        raise ValueError('Copy the existing store to the mounted volume before switching')
    if not os.path.exists(directory):
        subprocess.check_call(['sudo', 'install', '-d', '-m', '0700', '-o', user, directory])
    db = sqlite3.connect(path)
    try:
        fresh = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        if fresh and previous:
            raise ValueError('Mounted database is empty; expected the existing store')
        if fresh:
            db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
            db.execute('INSERT INTO volume VALUES(?)', (uuid.uuid4().hex,))
            db.commit()
        identity = db.execute('SELECT uuid FROM volume').fetchone()[0]
        if previous and previous != identity:
            raise ValueError('Volume identity differs from the configured store')
        return identity
    finally:
        db.close()


def main():
    user = pwd.getpwuid(os.getuid())
    if user.pw_uid == 0:
        raise SystemExit('Run the installer as the Pi controller owner.')
    os.umask(0o077)
    src = os.path.dirname(os.path.realpath(__file__))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', default=os.path.join(src, '..', '..'))
    parser.add_argument('--controller-home', default=os.environ.get('BIOME_HOME',
                        os.path.join(user.pw_dir, '.local/share/biomerte')))
    parser.add_argument('--existing-volume', help='Use an already mounted, boot-configured filesystem')
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    checkout = os.path.realpath(args.checkout)
    sys.path.insert(0, checkout)
    from biomerte.app import initialize, read_json
    from biomerte.worker import Remote
    home = os.path.join(user.pw_dir, '.local/share/biomerte-file-storage')
    manifest = os.path.join(home, 'manifest.json')
    old = read_json(manifest) if os.path.isfile(manifest) else {}
    if args.existing_volume:
        root = os.path.realpath(args.existing_volume)
        if root == '/':
            raise ValueError('Select a dedicated storage mount')
    else:
        root = '/srv/biomertefiles'
        if old and old['mount'] != root:
            raise ValueError('Reinstall with --existing-volume for the configured mount')
        subprocess.check_call(['/bin/sh', os.path.join(src, 'prepare-volume.sh')])
    volume = open_volume(root, user.pw_name, old.get('volume'))
    controller = read_json(os.path.join(args.controller_home, 'node.json'))
    url = 'http://127.0.0.1:%d' % (args.port or controller['port'])
    admin = Remote(url, controller['site'], controller['node'], None, token=controller['admin_token'])
    provider = initialize(home)
    resource = 'files.' + provider['node']
    adapter = os.path.join(home, 'storage.py')
    scopes = ['cap.offer', 'cap.fenced', 'provider.poll', 'provider.claim', 'provider.result',
              'cap.status', 'resource:' + resource]
    offers = []
    for op in sorted(OPERATIONS):
        inputs, description = OPERATIONS[op]
        name = 'storage.file.' + op
        contract = name + ':1'
        admin.call('cap.define', {'name': name, 'inputs': inputs, 'outputs': OUTPUTS,
                                  'description': description})
        scopes.append('offer:' + contract)
        offers.append({'contract': contract, 'resource': resource, 'service': 'file-storage',
                       'argv': [sys.executable, adapter, manifest, op]})
    admin.call('peer.trust', {'public_key': provider['public_key'], 'scopes': scopes,
                              'label': os.uname()[1] + ' file storage'})
    with open(os.path.join(src, 'storage.py'), 'rb') as stream:
        write_atomic(adapter, stream.read())
    cfg = {'url': url, 'site': controller['site'], 'offers': offers, 'mount': root,
           'volume': volume, 'poll_seconds': 1}
    write_atomic(manifest, json.dumps(cfg, indent=2).encode())
    mount_unit = subprocess.check_output(['systemd-escape', '--path', root]).decode().strip() + '.mount'
    unit = 'biomerte-file-storage.service'
    command = [os.path.join(checkout, 'biome'), '--home', home, 'worker', manifest]
    unit_text = ['[Unit]', 'Description=BiomeRTE Pi file storage', 'BindsTo=' + mount_unit,
                 'After=biomerte.service ' + mount_unit, 'Wants=biomerte.service',
                 '[Service]', 'User=' + user.pw_name, 'UMask=0077',
                 'ExecStartPre=/bin/mountpoint -q ' + service_arg(root),
                 'ExecStart=/usr/bin/env ' + ' '.join(service_arg(v) for v in command),
                 'Restart=always', 'RestartSec=3', '[Install]', 'WantedBy=multi-user.target', '']
    path = os.path.join(home, unit)
    write_atomic(path, '\n'.join(unit_text).encode())
    subprocess.check_call(['sudo', 'install', '-m', '0644', path, '/etc/systemd/system/' + unit])
    for args in (['daemon-reload'], ['enable', unit], ['restart', unit], ['is-active', unit]):
        subprocess.check_call(['sudo', 'systemctl'] + args)
    print('Installed %s using %s on %s' % (unit, root, resource))


if __name__ == '__main__':
    main()
