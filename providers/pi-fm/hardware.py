"""Clock cleanup for the inspected Pi 3 backend; never enables transmission."""
import mmap
import os
import struct
import time


class Clock:
    def __init__(self):
        with open('/proc/device-tree/model', 'rb') as stream:
            model = stream.read().rstrip(b'\0').decode('ascii')
        if model != 'Raspberry Pi 3 Model B Rev 1.2':
            raise RuntimeError('This hardware adapter is qualified only for ProximityRF Pi 3 Model B Rev 1.2')
        with open('/proc/device-tree/soc/ranges', 'rb') as stream:
            ranges = stream.read(12)
        if len(ranges) != 12 or struct.unpack('>III', ranges)[1] != 0x3f000000:
            raise RuntimeError('Unexpected peripheral mapping')
        fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
        try:
            self.gpio = mmap.mmap(fd, 4096, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=0x3f200000)
            self.clock = mmap.mmap(fd, 4096, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=0x3f101000)
        finally:
            os.close(fd)
        self.original = self.state()
        self.acquired = False

    def get(self, bank, offset):
        return struct.unpack_from('<I', bank, offset)[0]

    def put(self, bank, offset, value):
        struct.pack_into('<I', bank, offset, value)

    def state(self):
        ctl = self.get(self.clock, 0x70)
        return {'gpio4_function': (self.get(self.gpio, 0) >> 12) & 7,
                'clock_enabled': bool(ctl & 16), 'clock_busy': bool(ctl & 128),
                'clock_control': ctl & 0xffffff, 'clock_divisor': self.get(self.clock, 0x74) & 0xffffff}

    def acquire(self):
        s = self.state()
        if s['clock_enabled'] or s['clock_busy'] or s['gpio4_function'] != 0:
            raise RuntimeError('GPIO4/GPCLK0 is already configured or active; refusing to take another user\'s hardware')
        self.acquired = True
        return s

    def cleanup(self):
        if not self.acquired:
            return {'performed': False, 'reason': 'hardware not acquired'}
        # Disable our GPCLK0 first. Read-modify-write only GPIO4's function bits.
        self.put(self.clock, 0x70, 0x5a000000)
        deadline = time.monotonic() + .2
        while self.state()['clock_busy'] and time.monotonic() < deadline:
            time.sleep(.005)
        if self.state()['clock_busy']:
            self.put(self.clock, 0x70, 0x5a000020)
            time.sleep(.01)
        self.put(self.gpio, 0, (self.get(self.gpio, 0) & ~(7 << 12)) | (self.original['gpio4_function'] << 12))
        self.put(self.clock, 0x74, 0x5a000000 | self.original['clock_divisor'])
        self.put(self.clock, 0x70, 0x5a000000 | (self.original['clock_control'] & ~(16 | 32 | 128)))
        actual = self.state()
        return {'performed': True, 'verified': not actual['clock_enabled'] and not actual['clock_busy'] and actual['gpio4_function'] == self.original['gpio4_function'], 'after': actual}

    def close(self):
        self.gpio.close()
        self.clock.close()
