"""On-demand mono FM audio via the inspected Pi GPIO4 transmitter."""
import array
import glob
import json
import math
import os
import pwd
import signal
import stat
import subprocess
import sys
import time
import uuid
import wave
from common import BACKEND, BACKEND_SHA256, TERMINAL, atomic, digest, identity, read, request_digest, settings


class Transmitter:
    def __init__(self, config):
        self.root = config.get('root', os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.local/share/fm-transmitter'))
        self.allowed_audio_root = os.path.realpath(config.get('audio_root', pwd.getpwuid(os.getuid()).pw_dir))
        os.makedirs(self.root, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.processes = {}

    def capability(self):
        exists = os.path.isfile(BACKEND)
        return {'backend': BACKEND, 'backend_sha256': digest(BACKEND) if exists else None,
                'backend_matches': exists and digest(BACKEND) == BACKEND_SHA256,
                'frequency_range_hz': [88100000, 107900000], 'duration_range_seconds': [1, 120],
                'sources': ['tone', 'wav'], 'audio_format': 'mono PCM16 WAV, converted to 22050 Hz',
                'gpio': 4, 'physical_header_pin': 7, 'resource': 'GPIO4 / GPCLK0',
                'rf_power_control': False, 'default_volume': .2,
                'note': 'Volume changes modulation, not RF power. GPIO square-wave output requires a suitable filtered/contained RF setup. SDR detection is not a calibrated emissions-compliance measurement.'}

    def directories(self):
        return sorted(glob.glob(os.path.join(self.root, 'job-*')), key=lambda p: os.stat(p).st_mtime, reverse=True)

    def status_one(self, directory):
        record = read(os.path.join(directory, 'state.json'), {})
        if record.get('state') not in TERMINAL:
            proc = self.processes.get(record.get('job_id'))
            if proc is not None and proc.poll() is not None:
                # Re-read after observing exit: final file normally precedes it.
                record = read(os.path.join(directory, 'state.json'), record)
                if record.get('state') not in TERMINAL:
                    record.update(state='failed', error='Watchdog exited without terminal receipt; physical cleanup unverified', finished_at=time.time())
                    atomic(os.path.join(directory, 'state.json'), record)
            elif record.get('worker_identity') and identity(record['worker_identity']['pid']) != record['worker_identity']:
                record.update(state='failed', error='Watchdog is gone; cleanup unverified', finished_at=time.time())
                atomic(os.path.join(directory, 'state.json'), record)
            elif time.time() - record.get('started_at', time.time()) > 130:
                record.update(state='failed', error='Transmission deadline exceeded without receipt; inspect hardware', finished_at=time.time())
                atomic(os.path.join(directory, 'state.json'), record)
        return record

    def status(self, job_id=None):
        if job_id:
            try:
                if str(uuid.UUID(job_id)) != job_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('Invalid job_id')
            path = os.path.join(self.root, 'job-'+job_id)
            if not os.path.isdir(path):
                raise ValueError('Unknown job_id')
            return self.status_one(path)
        return {'as_of': time.time(), 'jobs': [self.status_one(p) for p in self.directories()[:20]], 'auto_resume': False}

    def prepare_audio(self, cfg, destination):
        duration = cfg['duration_seconds'] + 2
        if cfg['source'] == 'tone':
            with wave.open(destination, 'wb') as out:
                out.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                for start in range(0, int(22050*duration), 22050):
                    end = min(start+22050, int(22050*duration))
                    samples = array.array('h', (int(32767*cfg['volume']*math.sin(2*math.pi*cfg['tone_hz']*i/22050)) for i in range(start, end)))
                    if sys.byteorder != 'little':
                        samples.byteswap()
                    out.writeframesraw(samples.tobytes())
        else:
            path = cfg['audio_path']
            if not path.startswith(self.allowed_audio_root + os.sep):
                raise ValueError('WAV must be within configured audio_root')
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 64*1024*1024:
                raise ValueError('WAV must be a regular file no larger than 64 MiB')
            with wave.open(path, 'rb') as source:
                if source.getcomptype() != 'NONE' or source.getsampwidth() != 2 or source.getnchannels() not in (1,2) or not 8000 <= source.getframerate() <= 48000:
                    raise ValueError('Use PCM16 WAV, mono/stereo, 8000..48000 Hz')
            with open(os.devnull, 'wb') as out, open(destination+'.log', 'wb') as err:
                p = subprocess.Popen(['/usr/bin/sox', path, '-r', '22050', '-c', '1', '-b', '16', destination, 'trim', '0', str(duration), 'vol', str(cfg['volume'])], stdin=subprocess.DEVNULL, stdout=out, stderr=err)
                p.wait()
                if p.returncode:
                    raise RuntimeError('Audio conversion failed')
            with wave.open(destination, 'rb') as out:
                if out.getnframes() < 22050:
                    raise ValueError('Audio must contain at least one second')

    def transmit(self, args):
        cfg = settings(args)
        fingerprint = request_digest(cfg)
        paths = self.directories()
        for directory in paths:
            req = read(os.path.join(directory, 'request.json'), {})
            if req.get('settings', {}).get('request_id') == cfg['request_id']:
                if req.get('request_digest') != fingerprint:
                    raise ValueError('request_id reused with different inputs')
                return self.status_one(directory)
        if any(self.status_one(p).get('state') not in TERMINAL for p in paths):
            raise RuntimeError('FM transmitter busy; inspect status and stop its job explicitly')
        if len(paths) >= 1000:
            raise RuntimeError('Receipt capacity reached; archive records before new requests')
        if not self.capability()['backend_matches']:
            raise RuntimeError('Inspected backend is missing or changed')
        key = str(uuid.uuid4())
        directory = os.path.join(self.root, 'job-'+key)
        os.mkdir(directory, 0o700)
        job = {'job_id': key, 'settings': cfg, 'request_digest': fingerprint, 'parent': identity(os.getpid())}
        atomic(os.path.join(directory, 'request.json'), job)
        state = dict(job_id=key, settings=cfg, state='preparing', started_at=time.time())
        atomic(os.path.join(directory, 'state.json'), state)
        try:
            self.prepare_audio(cfg, os.path.join(directory, 'audio.wav'))
            with open(os.path.join(directory, 'watchdog.log'), 'wb') as log:
                p = subprocess.Popen(['/usr/bin/sudo', '-n', '/usr/bin/python3', os.path.join(os.path.dirname(__file__), 'worker.py'), directory], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            self.processes[key] = p
        except Exception as error:
            state.update(state='failed', error=str(error)[:1000], finished_at=time.time())
            atomic(os.path.join(directory, 'state.json'), state)
            try:
                os.unlink(os.path.join(directory, 'audio.wav'))
            except OSError:
                pass
            raise
        return self.status_one(directory)

    def stop(self, job_id):
        row = self.status(job_id)
        if row['state'] in TERMINAL:
            return row
        directory = os.path.join(self.root, 'job-'+job_id)
        atomic(os.path.join(directory, 'stop'), {'job_id': job_id, 'requested_at': time.time()})
        return {'job_id': job_id, 'state': 'stopping', 'note': 'Poll status for backend exit and clock cleanup receipt'}


def start(config):
    return Transmitter(config)


def health(context):
    return True


def handle(method, inputs, context):
    if method != 'call':
        raise ValueError('Unknown method')
    action = inputs.get('action')
    if action == 'capabilities':
        return context.capability()
    if action == 'status':
        return context.status(inputs.get('job_id'))
    if action == 'transmit':
        return context.transmit(inputs)
    if action == 'stop':
        return context.stop(inputs.get('job_id'))
    raise ValueError('Unknown action')
