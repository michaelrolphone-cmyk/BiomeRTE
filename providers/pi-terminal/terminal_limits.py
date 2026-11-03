"""Optional per-host policy, re-read when a terminal command starts (Python 3.4+)."""
import json
import os
import resource

RESOURCE_LIMITS = {'cpu_seconds': resource.RLIMIT_CPU, 'memory_bytes': resource.RLIMIT_AS,
                   'file_bytes': resource.RLIMIT_FSIZE, 'open_files': resource.RLIMIT_NOFILE}


def read_limits(home):
    try:
        with open(os.path.join(str(home), 'terminal-limits.json')) as source:
            limits = json.load(source)
    except FileNotFoundError:
        return {}
    if not isinstance(limits, dict):
        raise ValueError('Terminal limits must be a JSON object')
    for name, value in limits.items():
        if name not in RESOURCE_LIMITS and name != 'duration_seconds':
            raise ValueError('Unknown terminal limit: ' + name)
        if type(value) is not int or value <= 0:
            raise ValueError('Expected a positive integer for ' + name)
    return limits


def apply_limits(limits):
    for name, kind in RESOURCE_LIMITS.items():
        if name in limits:
            soft, hard = resource.getrlimit(kind)
            ceiling = min([limits[name]] + [v for v in (soft, hard) if v != resource.RLIM_INFINITY])
            resource.setrlimit(kind, (ceiling, ceiling))
