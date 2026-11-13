"""Frequency coverage policy and stable physical receiver identities (Python 3.4+)."""
import ctypes
import ctypes.util
import glob
import json
import math
import os
import threading

RTL_RANGE = [[25000000, 1750000000]]
ESP_RANGE = [[1850000000, 2750000000]]


def usb_rtl_paths(count):
    # librtlsdr walks libusb_get_device_list in this order. Fail closed if the
    # supported population differs; never attach antenna policy by guessed index.
    lib = ctypes.CDLL(ctypes.util.find_library('usb-1.0') or 'libusb-1.0.so.0')
    pointer = ctypes.c_void_p
    lib.libusb_init.argtypes = [ctypes.POINTER(pointer)]
    lib.libusb_get_device_list.argtypes = [pointer, ctypes.POINTER(ctypes.POINTER(pointer))]
    lib.libusb_get_device_list.restype = ctypes.c_ssize_t
    lib.libusb_get_device_descriptor.argtypes = [pointer, pointer]
    lib.libusb_get_bus_number.argtypes = [pointer]
    lib.libusb_get_port_numbers.argtypes = [pointer, pointer, ctypes.c_int]
    lib.libusb_free_device_list.argtypes = [ctypes.POINTER(pointer), ctypes.c_int]
    lib.libusb_exit.argtypes = [pointer]
    ctx = pointer()
    if lib.libusb_init(ctypes.byref(ctx)) < 0:
        raise RuntimeError('libusb initialization failed')
    listing = ctypes.POINTER(pointer)()
    try:
        n = lib.libusb_get_device_list(ctx, ctypes.byref(listing))
        if n < 0: raise RuntimeError('libusb enumeration failed')
        paths = []
        for i in range(n):
            descriptor = (ctypes.c_ubyte * 18)()
            if lib.libusb_get_device_descriptor(listing[i], descriptor) < 0: continue
            vid = descriptor[8] + descriptor[9]*256
            pid = descriptor[10] + descriptor[11]*256
            if (vid, pid) not in ((0x0bda, 0x2832), (0x0bda, 0x2838)): continue
            ports = (ctypes.c_ubyte * 8)()
            length = lib.libusb_get_port_numbers(listing[i], ports, 8)
            if length <= 0: raise RuntimeError('RTL USB port identity unavailable')
            paths.append(str(lib.libusb_get_bus_number(listing[i])) + '-' +
                         '.'.join(str(ports[j]) for j in range(length)))
        if len(paths) != count:
            raise RuntimeError('RTL/libusb identity population mismatch; stable routing unavailable')
        return paths
    finally:
        if listing: lib.libusb_free_device_list(listing, 1)
        lib.libusb_exit(ctx)


def normalize_serial(value):
    return ''.join(c for c in value.lower() if c in '0123456789abcdef')


def esp_port(serial):
    wanted = normalize_serial(serial)
    for path in glob.glob('/sys/bus/usb/devices/*'):
        try:
            with open(path+'/idVendor') as f: vendor = f.read().strip()
            with open(path+'/serial') as f: actual = normalize_serial(f.read().strip())
            if vendor != '303a' or actual != wanted: continue
            for port in glob.glob('/sys/class/tty/ttyACM*'):
                if os.path.realpath(port+'/device').startswith(os.path.realpath(path)+'/'):
                    return '/dev/'+os.path.basename(port)
        except OSError:
            pass
    return None


def ranges(value, hardware):
    if value is None: return None
    if not isinstance(value, list): raise ValueError('antenna_ranges_hz must be list or null')
    result = []
    for band in value:
        if not isinstance(band, list) or len(band) != 2:
            raise ValueError('Each antenna range is [lower_hz, upper_hz]')
        lo, hi = band
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in band):
            raise ValueError('Frequency bounds must be finite numbers')
        if lo >= hi or not any(a <= lo < hi <= b for a, b in hardware):
            raise ValueError('Antenna range outside receiver hardware coverage')
        result.append([int(lo), int(hi)])
    return sorted(result)


def matching(bands, low, high):
    return [b-a for a,b in bands if a <= low and high <= b]


class Catalog:
    def __init__(self, root, rtl_inventory, port_lookup=esp_port):
        self.path = os.path.join(root, 'receivers.json')
        self.rtl_inventory = rtl_inventory
        self.port_lookup = port_lookup
        self.guard = threading.RLock()
        try:
            with open(self.path) as f: self.config = json.load(f)
        except FileNotFoundError:
            self.config = {'revision': 0, 'receivers': {}}

    def inventory(self):
        with self.guard:
            cfg = json.loads(json.dumps(self.config))
        rows = self.rtl_inventory()
        for row in rows:
            row.update(driver='rtl', hardware_ranges_hz=RTL_RANGE, connected=True)
        known = set(row['id'] for row in rows)
        for identity, entry in cfg['receivers'].items():
            if entry.get('driver') == 'esp_sdr':
                port = self.port_lookup(entry['serial'])
                rows.append({'id': identity, 'driver': 'esp_sdr', 'serial': entry['serial'],
                             'port': port, 'connected': bool(port), 'hardware_ranges_hz': ESP_RANGE,
                             'aliases': entry.get('aliases', []), 'receive_only': True})
            elif identity not in known:
                rows.append({'id': identity, 'driver': 'rtl', 'connected': False,
                             'hardware_ranges_hz': RTL_RANGE, 'receive_only': True})
        for row in rows:
            entry = cfg['receivers'].get(row['id'], {})
            row.update(label=entry.get('label', row['id']), enabled=entry.get('enabled', True),
                       priority=entry.get('priority', 0), antenna_ranges_hz=entry.get('antenna_ranges_hz'),
                       routing_revision=cfg['revision'])
            row['effective_ranges_hz'] = (row['hardware_ranges_hz'] if row['antenna_ranges_hz'] is None
                                         else row['antenna_ranges_hz'])
            row.setdefault('aliases', [])
            if row.get('index') is not None: row['aliases'].append(str(row['index']))
            if row.get('unique_serial'): row['aliases'].append(row['serial'])
        return rows

    def select(self, selector, center, width, rows=None):
        rows = self.inventory() if rows is None else rows
        low, high = center-width/2.0, center+width/2.0
        if selector != 'auto':
            matches = [r for r in rows if selector.lower() in
                       [r['id'].lower()] + [a.lower() for a in r['aliases']]]
            if len(matches) != 1: raise ValueError('Receiver missing or ambiguous; inspect devices')
            selected = dict(matches[0])
            if not selected['connected'] or not selected['enabled']:
                raise ValueError('Selected receiver offline or disabled')
            if not matching(selected['hardware_ranges_hz'], low, high):
                raise ValueError('Requested window outside selected receiver hardware coverage')
            selected['selection_basis'] = 'explicit'
            if not matching(selected['effective_ranges_hz'], low, high):
                selected['antenna_warning'] = 'Explicit selection outside configured antenna coverage'
            return selected
        candidates = []
        for row in rows:
            if not row['connected'] or not row['enabled']: continue
            bands = matching(row['effective_ranges_hz'], low, high)
            if not bands: continue
            score = (row['antenna_ranges_hz'] is None, min(bands), -row['priority'], row['id'])
            candidates.append((score, row))
        if not candidates: raise ValueError('No enabled receiver covers the complete requested frequency window')
        selected = dict(min(candidates, key=lambda v:v[0])[1])
        selected['selection_basis'] = 'frequency_coverage'
        return selected

    def configure(self, inputs):
        with self.guard:
            if inputs.get('expected_revision') != self.config['revision']:
                raise ValueError('Receiver revision changed; read receivers first')
            selector = str(inputs.get('device', ''))
            rows = self.inventory()
            matches = [r for r in rows if selector.lower() in
                       [r['id'].lower()] + [a.lower() for a in r['aliases']]]
            if len(matches) != 1: raise ValueError('Receiver missing or ambiguous')
            row = matches[0]
            cfg = json.loads(json.dumps(self.config))
            entry = cfg['receivers'].setdefault(row['id'], {'driver': row['driver']})
            if 'antenna_ranges_hz' in inputs:
                entry['antenna_ranges_hz'] = ranges(inputs['antenna_ranges_hz'], row['hardware_ranges_hz'])
            if 'receiver_label' in inputs:
                value = inputs['receiver_label']
                if not isinstance(value, str) or not 1 <= len(value) <= 128:
                    raise ValueError('receiver_label must have 1..128 characters')
                entry['label'] = value
            if 'receiver_enabled' in inputs:
                if not isinstance(inputs['receiver_enabled'], bool): raise ValueError('receiver_enabled must be bool')
                entry['enabled'] = inputs['receiver_enabled']
            if 'receiver_priority' in inputs:
                value = inputs['receiver_priority']
                if isinstance(value, bool) or not isinstance(value, int) or not -1000 <= value <= 1000:
                    raise ValueError('receiver_priority must be integer -1000..1000')
                entry['priority'] = value
            cfg['revision'] += 1
            tmp = self.path+'.tmp'
            with open(tmp, 'w') as f: json.dump(cfg, f, sort_keys=True)
            os.replace(tmp, self.path)
            self.config = cfg
            return {'revision': cfg['revision'], 'devices': self.inventory()}
