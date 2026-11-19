#!/usr/bin/env python3
"""Pin an existing controller command to one physical release (Python 3.4+).

Keep this launcher outside the releases it selects. It does not initialize or
alter controller data, service settings, or the original command's arguments.
"""
from __future__ import print_function
import argparse
import json
import os
import sys


def launch(config):
    root = os.path.abspath(os.path.expanduser(config['root']))
    home = os.path.abspath(os.path.expanduser(config['home']))
    if not os.path.isfile(os.path.join(home, 'node.json')):
        raise ValueError('existing_node_required')
    current = os.path.join(root, 'current')
    if not os.path.islink(current):
        raise ValueError('current_symlink_required')
    release = os.path.realpath(current)
    if not os.path.isfile(os.path.join(release, 'biomerte', '__init__.py')):
        raise ValueError('controller_package_missing')
    command = config.get('controller_argv')
    if (not isinstance(command, list) or not command
            or not all(isinstance(part, str) for part in command)):
        raise ValueError('controller_argv_required')
    extra = config.get('extra_pythonpaths', [])
    if not isinstance(extra, list) or not all(isinstance(path, str) and os.path.isabs(path)
                                              for path in extra):
        raise ValueError('extra_pythonpaths_must_be_absolute')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join([release] + [
        os.path.realpath(path) for path in extra])
    os.chdir(release)
    # The native service's interpreter, -S, wrapper, --home and every argument
    # survive unchanged. exec preserves the service process identity.
    os.execvpe(command[0], command, environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    options = parser.parse_args(argv)
    with open(options.config, encoding='utf-8') as stream:
        config = json.load(stream)
    launch(config)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Do not expose command arguments, environment, credentials, or paths.
        known = ('existing_node_required', 'current_symlink_required',
                 'controller_package_missing', 'controller_argv_required',
                 'extra_pythonpaths_must_be_absolute')
        message = str(error) if str(error) in known else type(error).__name__
        sys.stderr.write(message + '\n')
        sys.exit(1)
