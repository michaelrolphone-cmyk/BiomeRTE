#!/usr/bin/env python3
"""Prepare an additive Linux maintenance installation; never activate services.

Run as the eventual service user. This writes a private dedicated credential,
configuration, pinned agent source and a reviewable systemd unit. It prints only
the credential verifier. Enroll and verify the heartbeat before activation.
Compatible with Python 3.4; no BiomeRTE imports, controller edits or restart calls.
"""
from __future__ import print_function

import argparse
import binascii
import hashlib
import json
import os
import pwd
import re
import sys

import linux_maintenance_agent as agent


def atomic_private(path, content):
    encoded = content.encode('utf-8')
    if os.path.lexists(path):
        if agent.private_file(path).encode('utf-8') != encoded:
            raise ValueError('Existing installation differs; stage and review an upgrade separately')
        return
    temporary = path + '.prepare-' + binascii.hexlify(os.urandom(8)).decode('ascii')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Never replace a concurrently installed credential or source.
        os.link(temporary, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def quote_unit(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid systemd argument')
    return '"' + value.replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"') + '"'


def unit_text(home, user, python):
    if not re.match(r'^[a-z_][a-z0-9_-]*[$]?$', user):
        raise ValueError('Invalid Linux service user')
    if not os.path.isabs(home) or not os.path.isabs(python):
        raise ValueError('Expected absolute installation and interpreter paths')
    arguments = [python, '-B', '-S', os.path.join(home, 'linux_maintenance_agent.py'),
                 home, '--runtime', '/run/biomerte-maintenance']
    return '\n'.join([
        '[Unit]',
        'Description=Independent BiomeRTE Linux maintenance',
        'Wants=network-online.target',
        'After=network-online.target',
        '',
        '[Service]',
        'Type=simple',
        'User=' + user,
        'UMask=0077',
        'RuntimeDirectory=biomerte-maintenance',
        'RuntimeDirectoryMode=0700',
        'WorkingDirectory=/run/biomerte-maintenance',
        'ExecStart=' + ' '.join(quote_unit(value) for value in arguments),
        'Restart=on-failure',
        'RestartSec=5',
        'KillMode=control-group',
        'TimeoutStopSec=infinity',
        'StandardOutput=null',
        'StandardError=null',
        '',
        '[Install]',
        'WantedBy=multi-user.target',
        '',
    ])


def prepare(home, origin, host, python, source=None):
    home = os.path.abspath(home)
    if not re.match(r'^[a-zA-Z0-9_.-]+$', host):
        raise ValueError('Invalid maintenance host')
    # Validate URL shape and exact old-Python verified TLS construction before
    # writing any credential. There is no network request or insecure fallback.
    agent.Transport(origin, '0' * 64, host)
    agent.require_durable_home(home)
    os.makedirs(home, mode=0o700, exist_ok=True)
    stat = os.stat(home)
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('Installation directory must be private to the service user')
    key_path = os.path.join(home, 'device.key')
    if os.path.exists(key_path):
        key = agent.private_file(key_path).strip()
        if not re.match(r'^[0-9a-f]{64}$', key):
            raise ValueError('Invalid existing device credential')
    else:
        key = binascii.hexlify(os.urandom(32)).decode('ascii')
        atomic_private(key_path, key + '\n')
    config = {'agent': agent.AGENT, 'origin': origin.rstrip('/'), 'host': host}
    atomic_private(os.path.join(home, 'config.json'),
                   json.dumps(config, sort_keys=True, indent=2) + '\n')
    if source is None:
        source = agent.read_text(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             'linux_maintenance_agent.py'))
    compile(source, 'linux_maintenance_agent.py', 'exec')
    atomic_private(os.path.join(home, 'linux_maintenance_agent.py'), source)
    user = pwd.getpwuid(os.getuid()).pw_name
    unit = os.path.join(home, 'biomerte-maintenance.service')
    atomic_private(unit, unit_text(home, user, python))
    return {'host': host, 'key_hash': hashlib.sha256(key.encode('ascii')).hexdigest(),
            'agent': agent.AGENT, 'home': home, 'unit': unit,
            'source_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
            'activated': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--python', default='/usr/bin/python3')
    args = parser.parse_args(argv)
    if not sys.platform.startswith('linux'):
        raise ValueError('Linux installation preparation must run on Linux')
    if os.getuid() == 0:
        raise ValueError('Prepare as the unprivileged service user; install the reviewed unit separately')
    os.umask(0o077)
    print(json.dumps(prepare(args.home, args.origin, args.host, args.python),
                     sort_keys=True))


if __name__ == '__main__':
    main()
