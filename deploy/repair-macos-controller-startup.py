#!/usr/bin/env python3
"""Persist the qualified Python -S startup repair in existing controller agents.

Run with Python -S. Preview by default; --apply backs up and replaces the plists.
Services are not restarted by this command.
"""
import argparse
import datetime
import os
import plistlib
import tempfile
from pathlib import Path

LABELS = ('org.biomerte.controller', 'org.biomerte.controller-link')


def repair(directory, apply=False):
    changes = []
    for label in LABELS:
        path = Path(directory) / (label + '.plist')
        original = path.read_bytes()
        data = plistlib.loads(original)
        args = data.get('ProgramArguments', [])
        if not args or not Path(args[0]).name.startswith('python'):
            raise ValueError('Expected a direct Python executable in ' + str(path))
        if '-S' in args[1:]:
            continue
        data['ProgramArguments'] = [args[0], '-S'] + args[1:]
        changes.append((path, original, plistlib.dumps(data)))
    # Validate both agents before writing either one.
    if apply:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        for path, original, updated in changes:
            backup = path.with_name(path.name + '.before-no-site.' + stamp)
            with backup.open('xb') as stream:
                stream.write(original)
            os.chmod(backup, path.stat().st_mode & 0o777)
            fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(updated)
                os.chmod(temporary, path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    return [str(path) for path, _, _ in changes]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launchagents', type=Path,
                        default=Path.home() / 'Library/LaunchAgents')
    parser.add_argument('--apply', action='store_true')
    options = parser.parse_args()
    for changed in repair(options.launchagents, options.apply):
        print(('Updated ' if options.apply else 'Would update ') + changed)
