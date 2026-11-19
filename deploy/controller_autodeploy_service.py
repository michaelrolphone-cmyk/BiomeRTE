#!/usr/bin/env python3
"""Restart only the configured native controller service (Python 3.4+).

This recovery helper has no controller dependency and can bootstrap an unloaded
launchd job. It does not change service files, credentials, or provider jobs.
"""
from __future__ import print_function
import argparse
import json
import os
import re
import subprocess
import sys


class ServiceError(Exception):
    pass


def run_command(argv, timeout, check=True):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise ServiceError('native_service_command_timeout')
    if check and process.returncode:
        raise ServiceError('native_service_exit_' + str(process.returncode))
    return process.returncode


def restart(config, runner=None):
    runner = runner or run_command
    service = config['service']
    timeout = config.get('command_timeout', 60)
    manager = service.get('manager')
    if manager == 'launchd':
        label = service.get('label', '')
        domain = service.get('domain', '')
        plist = service.get('plist', '')
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*$', label):
            raise ServiceError('invalid_launchd_label')
        if not (domain == 'system' or re.match(r'^(gui|user)/[0-9]+$', domain)):
            raise ServiceError('invalid_launchd_domain')
        if not os.path.isabs(plist) or not os.path.isfile(plist):
            raise ServiceError('existing_launchd_plist_required')
        launchctl = service.get('launchctl', '/bin/launchctl')
        target = domain + '/' + label
        loaded = runner([launchctl, 'print', target], timeout, check=False) == 0
        if loaded and service.get('reload_on_restart', False):
            runner([launchctl, 'bootout', target], timeout)
            loaded = False
        if not loaded:
            runner([launchctl, 'bootstrap', domain, plist], timeout)
        runner([launchctl, 'kickstart', '-k', target], timeout)
    elif manager == 'systemd':
        unit = service.get('unit', '')
        if not re.match(r'^[A-Za-z0-9_.@:-]+\.service$', unit):
            raise ServiceError('invalid_systemd_unit')
        prefix = [service.get('systemctl', '/bin/systemctl')]
        if service.get('sudo', False):
            prefix = [service.get('sudo_binary', '/usr/bin/sudo'), '-n'] + prefix
        runner(prefix + ['daemon-reload'], timeout)
        runner(prefix + ['restart', unit], timeout)
    else:
        raise ServiceError('unknown_service_manager')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    options = parser.parse_args(argv)
    with open(options.config, encoding='utf-8') as stream:
        config = json.load(stream)
    restart(config)


if __name__ == '__main__':
    try:
        main()
    except (ServiceError, OSError, ValueError, TypeError, KeyError) as error:
        # Native stderr/arguments can expose host details. Keep failures typed.
        sys.stderr.write((str(error) if isinstance(error, ServiceError)
                          else type(error).__name__) + '\n')
        sys.exit(1)
