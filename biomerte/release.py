"""Process-local deployment identity, captured before the source link can move."""
import json
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_COMMIT = None
try:
    with open(os.path.join(_ROOT, '.biomerte-release.json'), encoding='utf-8') as _stream:
        _marker = json.load(_stream)
    if (_marker.get('schema') == 'biomerte-autodeploy-release/1'
            and re.match(r'^[0-9a-f]{40}$', _marker.get('commit', ''))):
        _COMMIT = _marker['commit']
except (OSError, ValueError, TypeError):
    pass


def identity():
    return {'commit': _COMMIT, 'pid': os.getpid()}
