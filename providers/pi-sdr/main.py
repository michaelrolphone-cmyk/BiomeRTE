"""Generic receive-only SDR service, Python 3.4+, librtlsdr and NumPy."""
import base64
import collections
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import math
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import routing

GARAGE_US = [300000000, 310000000, 315000000, 318000000, 372500000, 390000000, 433920000]
LIMIT_NOTE = 'Single tuner: other frequencies are unobserved during each dwell. RF activity is not door position or a decoded opener identity.'


def atomic(path, value):
    tmp = path + '.tmp'
    with open(tmp, 'w') as out:
        json.dump(value, out, sort_keys=True)
    os.replace(tmp, path)


def load(path, default):
    try:
        with open(path) as source:
            return json.load(source)
    except (OSError, ValueError):
        return default


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name + ' outside bounds')
    return value


def settings(inputs):
    frequencies = inputs.get('frequencies_hz')
    if frequencies is None and inputs.get('profile') == 'garage_us':
        frequencies = GARAGE_US
    if frequencies is None and 'frequency_hz' in inputs:
        frequencies = [inputs['frequency_hz']]
    if not isinstance(frequencies, list) or not 1 <= len(frequencies) <= 32:
        raise ValueError('Supply 1..32 frequencies_hz or profile=garage_us')
    result = {'frequencies_hz': [int(number(f, 25000000, 2750000000, 'frequency')) for f in frequencies],
              'device': str(inputs.get('device', 'auto')), 'gain_db': number(inputs.get('gain_db', 28), 0, 49.6, 'gain'),
              'ppm': int(number(inputs.get('ppm', 0), -200, 200, 'ppm')),
              'dwell_seconds': number(inputs.get('dwell_seconds', .25), .1, 2, 'dwell'),
              'width_hz': int(number(inputs.get('width_hz', 200000), 10000, 1000000, 'width')),
              'threshold_db': number(inputs.get('threshold_db', 12), 3, 50, 'threshold')}
    if not result['device'] or len(result['device']) > 64 or not all(c.isalnum() or c in '_-:.' for c in result['device']):
        raise ValueError('device must be a numeric index or exact USB serial')
    if result['dwell_seconds'] * len(frequencies) > 120:
        raise ValueError('Scan dwell budget exceeds 120 seconds')
    for f in result['frequencies_hz']:
        if f - result['width_hz']//2 < 25000000 or f + result['width_hz']//2 > 2750000000:
            raise ValueError('Scan window outside tuner range')
    result['gain_db_explicit'] = inputs.get('gain_db_explicit', 'gain_db' in inputs)
    if 'gain_index' in inputs:
        result['gain_index'] = int(number(inputs['gain_index'], 0, 82, 'gain_index'))
    return result


def devices():
    lib = ctypes.CDLL(ctypes.util.find_library('rtlsdr') or 'librtlsdr.so.0')
    lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
    lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
    rows = []
    for index in range(min(lib.rtlsdr_get_device_count(), 16)):
        manufacturer, product, serial = [ctypes.create_string_buffer(256) for unused in range(3)]
        rc = lib.rtlsdr_get_device_usb_strings(index, manufacturer, product, serial)
        rows.append({'index': index, 'name': (lib.rtlsdr_get_device_name(index) or b'').decode('utf8', 'replace'),
                     'manufacturer': manufacturer.value.decode('utf8', 'replace'),
                     'product': product.value.decode('utf8', 'replace'), 'serial': serial.value.decode('utf8', 'replace'),
                     'usb_strings_ok': rc == 0, 'receive_only': True})
    paths = routing.usb_rtl_paths(len(rows))
    for row, path in zip(rows, paths):
        row.update(id='rtl:usb:'+path, usb_path=path, aliases=[])
    counts = collections.Counter(r['serial'] for r in rows)
    for row in rows:
        row['unique_serial'] = bool(row['serial']) and counts[row['serial']] == 1
    return rows


def resolve_device(selector):
    rows = devices()
    if selector.isdigit() and len(selector) < 3:
        found = [r for r in rows if r['index'] == int(selector)]
    else:
        found = [r for r in rows if r['serial'] == selector]
    if len(found) != 1:
        raise ValueError('Receiver missing or serial ambiguous; inspect devices')
    return found[0]



class SDR:
    def __init__(self, config):
        self.root = config.get('root', os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.local/share/sdr-receiver'))
        os.makedirs(self.root, exist_ok=True)
        self.capture = os.path.join(os.path.dirname(__file__), 'capture.py')
        self.catalog = routing.Catalog(self.root, devices)
        self.jobs = collections.OrderedDict()
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.monitor_thread = None
        self.monitor = load(os.path.join(self.root, 'monitor.json'), {'enabled': False, 'revision': 0})
        self.latest = load(os.path.join(self.root, 'latest.json'), {})
        self.launch_monitor()

    def event(self, row):
        row = dict(row, timestamp=time.time())
        raw = json.dumps(row, sort_keys=True) + '\n'
        with self.guard:
            path = os.path.join(self.root, 'events.jsonl')
            if os.path.exists(path) and os.path.getsize(path) + len(raw) > 1024*1024:
                for n in range(2, 0, -1):
                    old = path + '.' + str(n)
                    if os.path.exists(old):
                        os.replace(old, path + '.' + str(n+1))
                os.replace(path, path + '.1')
            with open(path, 'a') as out:
                out.write(raw)

    def run(self, argv, timeout):
        # Disk-backed output avoids PIPE deadlocks. The child has a finite bin
        # and sample budget; parent kills hung USB I/O.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            p = subprocess.Popen(argv, stdout=out, stderr=err)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
                raise RuntimeError('SDR command timed out; USB device released after child termination')
            out.seek(0); err.seek(0)
            output, error = out.read(262144), err.read(8192)
            if p.returncode:
                raise RuntimeError('SDR command failed {}: {}'.format(p.returncode, error.decode('utf8','replace')[-2000:]))
            return output.decode('utf8', 'replace'), error.decode('utf8', 'replace')

    def lock_device(self, device):
        key = hashlib.sha256(device['id'].encode()).hexdigest()[:24]
        lock = open(os.path.join(self.root, 'device-'+key+'.lock'), 'a')
        deadline = time.time()+20
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                if time.time() >= deadline:
                    lock.close()
                    raise RuntimeError('Receiver busy')
                time.sleep(.05)

    def plan(self, cfg):
        rows = self.catalog.inventory()
        plan = [self.catalog.select(cfg['device'], f, cfg['width_hz'], rows) for f in cfg['frequencies_hz']]
        for receiver in plan:
            if receiver['driver'] == 'esp_sdr' and (cfg.get('gain_db_explicit') or cfg['ppm']):
                raise ValueError('ESP-SDR uses gain_index (0..82), not gain_db; ppm correction unsupported')
            if receiver['driver'] == 'rtl' and 'gain_index' in cfg:
                raise ValueError('RTL receiver uses gain_db, not gain_index')
        return plan

    def window(self, cfg, center, device=None):
        device = device or self.catalog.select(cfg['device'], center, cfg['width_hz'])
        with self.lock_device(device):
            spec = dict(cfg, device=device, frequency_hz=center)
            script = self.capture if device['driver'] == 'rtl' else os.path.join(os.path.dirname(__file__), 'esp_capture.py')
            argv = [sys.executable, script, json.dumps(spec)]
            started = time.time()
            output, stderr = self.run(argv, cfg['dwell_seconds']+15)
            row = json.loads(output)
            row.update({'observed_start': started, 'observed_end': time.time(),
                        'receiver_log': stderr[-1000:], 'limitations': LIMIT_NOTE,
                        'receiver_id': device['id'], 'driver': device['driver'],
                        'selection_basis': device['selection_basis'], 'routing_revision': device['routing_revision']})
            return row

    def scan(self, inputs):
        cfg = settings(inputs)
        cfg['include_iq'] = inputs.get('include_iq', False) is True
        plan = self.plan(cfg)
        with self.guard:
            if sum(j['state'] == 'running' for j in self.jobs.values()) >= 5:
                raise RuntimeError('At most five bounded scans may run concurrently')
            while len(self.jobs) >= 16:
                removable = next((k for k,v in self.jobs.items() if v['state'] != 'running'), None)
                if removable is None: raise RuntimeError('Scan history busy')
                del self.jobs[removable]
            key = str(uuid.uuid4())
            job = {'scan_id': key, 'state': 'running', 'settings': cfg, 'routing_plan': plan, 'started_at': time.time(), 'results': []}
            self.jobs[key] = job
        def work():
            try:
                for f, receiver in zip(cfg['frequencies_hz'], plan):
                    row = self.window(cfg, f, receiver)
                    with self.guard: job['results'].append(row)
                with self.guard: job['state'] = 'completed'
            except Exception as error:
                with self.guard: job.update(state='failed', error=str(error))
            finally:
                with self.guard: job['finished_at'] = time.time()
        thread = threading.Thread(target=work); thread.daemon = True; thread.start()
        return {'scan_id': key, 'state': 'running', 'result_method': 'results', 'limitations': LIMIT_NOTE}

    def launch_monitor(self):
        self.stop.clear()
        def work():
            while not self.stop.is_set():
                if not self.monitor.get('enabled'):
                    self.stop.wait(.5)
                    continue
                cycle = time.time(); cfg = self.monitor['settings']; rev = self.monitor['revision']
                for f in cfg['frequencies_hz']:
                    if self.stop.is_set() or not self.monitor.get('enabled') or self.monitor['revision'] != rev: break
                    try:
                        receiver = self.plan(dict(cfg, frequencies_hz=[f]))[0]
                        row = self.window(cfg, f, receiver)
                        summary = {k:v for k,v in row.items() if k not in ('bins','receiver_log','block_power_dbfs','iq_base64')}
                        with self.guard:
                            previous = self.latest.get(str(f), {})
                            if 'sample_finished_at' in previous:
                                summary['unobserved_gap_seconds'] = max(0, row['sample_started_at']-previous['sample_finished_at'])
                            self.latest[str(f)] = summary
                        if row['activity_candidate']:
                            self.event(dict(summary, kind='rf_activity_candidate'))
                    except Exception as error:
                        with self.guard: self.latest[str(f)] = {'error': str(error), 'observed_end': time.time()}
                        self.event({'kind':'receiver_error','frequency_hz':f,'error':str(error)})
                        if self.stop.wait(5): break
                    # Yield to interactive operations between windows.
                    if self.stop.wait(.1): break
                with self.guard:
                    atomic(os.path.join(self.root,'latest.json'),self.latest)
                    self.monitor['last_cycle_seconds'] = time.time()-cycle
                self.stop.wait(.5)
        self.monitor_thread = threading.Thread(target=work); self.monitor_thread.daemon = True; self.monitor_thread.start()

    def configure(self, inputs, enabled):
        revision = inputs.get('expected_revision')
        if revision != self.monitor['revision']:
            raise ValueError('Monitor revision changed; read status first')
        cfg = settings(inputs) if enabled else self.monitor.get('settings')
        if enabled:
            plan = self.plan(cfg)
            if cfg['device'] != 'auto': cfg['device'] = plan[0]['id']
        self.monitor = {'enabled': enabled, 'settings': cfg, 'revision': revision+1, 'updated_at': time.time()}
        atomic(os.path.join(self.root,'monitor.json'),self.monitor)
        return dict(self.monitor)

    def events(self, inputs):
        limit = int(number(inputs.get('limit',50),1,200,'limit'))
        since = number(inputs.get('since',0),0,1e12,'since')
        rows = collections.deque(maxlen=limit)
        for suffix in ('.3','.2','.1',''):
            try:
                with open(os.path.join(self.root,'events.jsonl')+suffix) as source:
                    for line in source:
                        try:
                            row = json.loads(line)
                            if row.get('timestamp',0) >= since: rows.append(row)
                        except ValueError: pass
            except FileNotFoundError: pass
        # Preserve newest events while keeping the complete RPC below 256 KiB.
        while rows and len(json.dumps(list(rows))) > 180000:
            rows.popleft()
        return {'events':list(rows),'retention':'4 x 1 MiB rotating files','limitations':LIMIT_NOTE}


def start(config): return SDR(config)

def health(ctx): return os.path.isfile(ctx.capture) and ctx.monitor_thread.is_alive()

def handle(method, inputs, ctx):
    if method == 'call':
        method = inputs.get('action')
    if method in ('devices', 'receivers'):
        return {'devices':ctx.catalog.inventory(), 'revision':ctx.catalog.config['revision'], 'receive_only':True}
    if method in ('receiver_configure','receiver_reset_antenna'):
        if method == 'receiver_reset_antenna': inputs = dict(inputs, antenna_ranges_hz=None)
        return ctx.catalog.configure(inputs)
    if method in ('scan','sample'): return ctx.scan(inputs)
    if method == 'monitor_start': return ctx.configure(inputs,True)
    if method == 'monitor_stop': return ctx.configure(inputs,False)
    if method == 'events': return ctx.events(inputs)
    if method == 'mark':
        label = inputs.get('label')
        if label not in ('opening','opened','closing','closed','remote_pressed','test_start','test_end'):
            raise ValueError('Unknown physical trial label')
        row = {'kind':'user_observation','label':label,'note':str(inputs.get('note',''))[:256]}
        ctx.event(row); return row
    with ctx.guard:
        if method == 'status':
            return {'monitor':dict(ctx.monitor),'monitor_thread_alive':bool(ctx.monitor_thread and ctx.monitor_thread.is_alive()),
                    'latest':dict(ctx.latest),'scans':[{'scan_id':k,'state':v['state']} for k,v in ctx.jobs.items()],
                    'as_of':time.time(),'limitations':LIMIT_NOTE,'door_state':'unknown; physical trial not yet performed'}
        if method == 'results':
            key = inputs.get('scan_id')
            if key not in ctx.jobs: raise ValueError('Scan missing/expired; last 16 retained, restart clears transient scans')
            # Bounded one-window pages keep native service transport under 256KiB.
            job = ctx.jobs[key]; offset = int(number(inputs.get('offset',0),0,31,'offset'))
            return dict({k:v for k,v in job.items() if k != 'results'}, results=job['results'][offset:offset+1],
                        next_offset=offset+1 if offset+1<len(job['results']) else None,
                        completed_windows=len(job['results']),limitations=LIMIT_NOTE)
    raise ValueError('Unsupported SDR method')
