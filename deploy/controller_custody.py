#!/usr/bin/env python3
"""Native source custody checks for independently installed recovery (Python 3.4+).

This module never imports a candidate package or opens a controller database.
Protocol declarations are a release compatibility contract, not authorization to
write: the running Application, Store and event engine retain their own guards.
"""
from __future__ import print_function
import ast
import errno
import json
import os
import re


class CustodyError(ValueError):
    def __init__(self, code):
        self.code = code
        ValueError.__init__(self, code)


def read_json(path):
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)


def read_custody(home):
    """An absent marker preserves legacy behavior; every invalid marker fails."""
    home = os.path.abspath(os.path.expanduser(home))
    path = os.path.join(home, 'controller-authority.json')
    try:
        os.lstat(path)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise CustodyError('custody_marker_unreadable')
    try:
        if os.path.islink(path):
            raise CustodyError('custody_marker_invalid')
        marker = read_json(path)
        identity = read_json(os.path.join(home, 'node.json'))
        if not isinstance(marker, dict) or marker.get('mode') != 'frozen-source':
            raise CustodyError('custody_native_source_required')
        required = set(('format', 'mode', 'source_node', 'site', 'service_id',
                        'authority_id', 'controller_aliases', 'migration_id',
                        'source_digest'))
        aliases = marker.get('controller_aliases')
        if (set(marker) != required or type(marker.get('format')) is not int
                or marker['format'] != 1
                or any(not isinstance(marker.get(key), str) or not marker[key]
                       for key in ('source_node', 'site', 'service_id',
                                   'authority_id', 'migration_id', 'source_digest'))
                or marker['source_node'] != identity['node']
                or marker['site'] != identity['site']
                or not isinstance(aliases, list)
                or not all(isinstance(alias, str) and alias for alias in aliases)
                or len(set(aliases)) != len(aliases)
                or marker['source_node'] not in aliases
                or marker['authority_id'] in aliases
                or not re.match(r'^[0-9a-f]{64}$', marker['source_digest'])):
            raise CustodyError('custody_marker_invalid')
        return marker
    except CustodyError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise CustodyError('custody_marker_invalid')


def require_protocol(release, relative):
    """Read a literal declaration without executing any selected release code."""
    path = os.path.join(release, relative)
    if (not os.path.isfile(path) or os.path.islink(path)
            or not os.path.realpath(path).startswith(release + os.sep)):
        raise CustodyError('custody_release_incompatible')
    try:
        with open(path, encoding='utf-8') as stream:
            tree = ast.parse(stream.read(), filename=relative)
        declarations = [node for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name)
                                and target.id == 'CUSTODY_PROTOCOL'
                                for target in node.targets)]
        if len(declarations) != 1:
            raise ValueError()
        value = ast.literal_eval(declarations[0].value)
        if type(value) is not int or value != 1:
            raise ValueError()
    except (OSError, SyntaxError, ValueError, TypeError):
        raise CustodyError('custody_release_incompatible')


def command_entry(config, release):
    """Recognize the actual pinned native and gateway commands, never wrappers."""
    command = config.get('controller_argv')
    if (not isinstance(command, list) or len(command) < 4
            or not all(isinstance(part, str) and part for part in command)
            or not re.match(r'^python(?:[0-9]+(?:\.[0-9]+)*)?$',
                            os.path.basename(command[0]))):
        raise CustodyError('custody_command_unverified')
    index = 1
    options = []
    while index < len(command) and command[index] in ('-S', '-B', '-u'):
        options.append(command[index])
        index += 1
    if '-S' not in options or index >= len(command):
        raise CustodyError('custody_command_unverified')
    if command[index:index + 2] == ['-m', 'biomerte']:
        entry, arguments = None, command[index + 2:]
        # The native entrypoint accepts one global home followed by serve.
        if len(arguments) != 3 or arguments[0] != '--home' or arguments[2] != 'serve':
            raise CustodyError('custody_command_unverified')
    else:
        entry = os.path.realpath(os.path.join(release, command[index]))
        allowed = [os.path.join(release, 'deploy', 'gateway-agent.py'),
                   os.path.join(release, 'gateway', 'deploy', 'gateway-agent.py')]
        if entry not in allowed:
            raise CustodyError('custody_command_unverified')
        arguments = command[index + 1:]
    positions = [i for i, argument in enumerate(arguments)
                 if argument == '--home' or argument.startswith('--home=')]
    if len(positions) != 1:
        raise CustodyError('custody_command_home_mismatch')
    position = positions[0]
    if arguments[position] == '--home':
        if position + 1 >= len(arguments):
            raise CustodyError('custody_command_home_mismatch')
        home = arguments[position + 1]
    else:
        home = arguments[position].split('=', 1)[1]
    if not os.path.isabs(home) or os.path.realpath(home) != os.path.realpath(config['home']):
        raise CustodyError('custody_command_home_mismatch')
    return entry


def require_release(config, release=None):
    """Fence candidates and old rollback targets before imports, links or exec."""
    marker = read_custody(config['home'])
    if marker is None:
        return None
    if release is None:
        current = os.path.join(os.path.abspath(os.path.expanduser(config['root'])),
                               'current')
        if not os.path.islink(current):
            raise CustodyError('custody_current_symlink_required')
        release = os.path.realpath(current)
    else:
        release = os.path.realpath(release)
    require_protocol(release, os.path.join('biomerte', 'app.py'))
    require_protocol(release, os.path.join('biomerte', 'store.py'))
    entry = command_entry(config, release)
    if entry is not None:
        directory = os.path.dirname(os.path.relpath(entry, release))
        require_protocol(release, os.path.join(directory, 'gateway-agent.py'))
        require_protocol(release, os.path.join(directory, 'mcp_events.py'))
    # A native candidate may also restart a paired gateway. Reject incompatible
    # bundled gateway code before switching the pointer, not after that restart.
    for directory in ('deploy', os.path.join('gateway', 'deploy')):
        if os.path.lexists(os.path.join(release, directory, 'gateway-agent.py')):
            require_protocol(release, os.path.join(directory, 'gateway-agent.py'))
            require_protocol(release, os.path.join(directory, 'mcp_events.py'))
    if read_custody(config['home']) != marker:
        raise CustodyError('custody_marker_changed')
    return marker


def site_readiness(home, invoke):
    """Prove the permitted source read path without asking it to execute work."""
    marker = read_custody(home)
    if marker is None:
        value = invoke('site', 'system.discovery', {'section': 'guide'})
        if (value.get('section') != 'guide' or not isinstance(value.get('items'), list)
                or not value['items']):
            raise CustodyError('site_request_path_failed')
    else:
        value = invoke('site', 'cluster.status', {})
        expected = {'mode': 'managed-source', 'node': marker['source_node'],
                    'automatic_failover': False, 'site_writes': 'frozen',
                    'service_id': marker['service_id'],
                    'authority_id': marker['authority_id']}
        if (not isinstance(value, dict)
                or any(value.get(key) != item for key, item in expected.items())
                or value.get('automatic_failover') is not False):
            raise CustodyError('custody_source_readiness_mismatch')
    if read_custody(home) != marker:
        raise CustodyError('custody_marker_changed')
    return value
