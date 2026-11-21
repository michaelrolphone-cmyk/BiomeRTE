"""Bounded FM transmission helpers, compatible with Python 3.4."""
import hashlib
import json
import math
import os
import re
import time

BACKEND_SHA256 = '8739363fd1e0cd557947015c11d90c7b8c4de0fbabdde8471e63697ce54edb53'
BACKEND = '/home/pi/Downloads/fm/fm_transmitter-master/fm_transmitter'
# Images supply their own immutable compiled backend manifest. Native installations
# retain the exact inspected executable/hash above when no manifest is configured.
_backend_manifest = os.environ.get('BIOMERTE_FM_BACKEND_MANIFEST')
if _backend_manifest:
    with open(_backend_manifest) as _stream:
        _backend = json.load(_stream)
    if set(_backend) != {'path', 'sha256'} or not isinstance(_backend['path'], str) or not os.path.isabs(_backend['path']) or not isinstance(_backend['sha256'], str) or not re.match(r'^[0-9a-f]{64}$', _backend['sha256']):
        raise ValueError('Invalid image FM backend manifest')
    BACKEND = _backend['path']
    BACKEND_SHA256 = _backend['sha256']
TERMINAL = ('completed', 'stopped', 'interrupted', 'failed')


def atomic(path, value):
    tmp = path + '.tmp-' + str(os.getpid())
    with open(tmp, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    # Root watchdog reports contain no credentials and must be readable by pi.
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read(path, default=None):
    try:
        with open(path) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def identity(pid):
    try:
        with open('/proc/{}/stat'.format(pid)) as stream:
            tail = stream.read().rsplit(')', 1)[1].split()
        if tail[0] == 'Z':
            return None
        with open('/proc/sys/kernel/random/boot_id') as stream:
            boot = stream.read().strip()
        return {'pid': int(pid), 'start_ticks': int(tail[19]), 'boot_id': boot}
    except (OSError, ValueError, IndexError):
        return None


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name + ' outside bounds')
    return value


def settings(args):
    allowed = {'action', 'request_id', 'frequency_hz', 'duration_seconds', 'source', 'tone_hz', 'audio_path', 'volume'}
    if set(args) - allowed:
        raise ValueError('Unknown transmission input')
    key = args.get('request_id', '')
    if not isinstance(key, str) or not re.match(r'^[a-zA-Z0-9_-]{1,64}$', key):
        raise ValueError('request_id must contain 1..64 letters, digits, _ or -')
    frequency = number(args.get('frequency_hz'), 88100000, 107900000, 'frequency_hz')
    if int(frequency) != frequency:
        raise ValueError('frequency_hz must be an integer')
    cfg = {'request_id': key, 'frequency_hz': int(frequency),
           'duration_seconds': number(args.get('duration_seconds'), 1, 120, 'duration_seconds'),
           'source': args.get('source', 'tone'),
           'volume': number(args.get('volume', .2), 0, 1, 'volume')}
    if cfg['source'] == 'tone':
        if 'audio_path' in args:
            raise ValueError('audio_path is only valid for wav')
        cfg['tone_hz'] = number(args.get('tone_hz', 1000), 100, 10000, 'tone_hz')
    elif cfg['source'] == 'wav':
        if 'tone_hz' in args:
            raise ValueError('tone_hz is only valid for tone')
        path = args.get('audio_path')
        if not isinstance(path, str) or not os.path.isabs(path) or len(path) > 512:
            raise ValueError('audio_path must be a local absolute WAV path')
        cfg['audio_path'] = os.path.realpath(path)
    else:
        raise ValueError('source must be tone or wav')
    return cfg


def request_digest(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode('utf8')).hexdigest()
