"""Bounded ESP-SDR protocol-6 USB burst capture, Python 3.4 / pyserial 2.6."""
import base64
import json
import math
import sys
import time
import zlib
import numpy as np
import routing
from capture import spectrum


class Protocol:
    def __init__(self, port):
        self.port = port

    def line(self, prefixes, seconds=4):
        deadline = time.monotonic()+seconds
        data = bytearray()
        while time.monotonic() < deadline:
            part = self.port.read(1)
            if not part: continue
            data.extend(part)
            if len(data) > 65536: raise RuntimeError('ESP response framing overflow')
            if part == b'\n':
                raw = bytes(data).rstrip(b'\r\n')
                # A disconnected client may leave an IQ tail before the next
                # ASCII reply. Only accept known prefixes, never arbitrary OK.
                for prefix in prefixes:
                    index = raw.find(prefix)
                    if index >= 0: return raw[index:]
                if raw.startswith(b'ERR '): raise RuntimeError(raw.decode('ascii', 'replace'))
                data = bytearray()
        raise RuntimeError('ESP response timeout')

    def command(self, command, prefix=b'OK'):
        self.port.write(command.encode('ascii')+b'\n')
        return self.line([prefix])

    def burst(self, count=12288):
        header = self.command('CAP16 {} 6'.format(count), b'DATA ')
        fields = header.split()
        if len(fields) != 4 or int(fields[1]) != count: raise RuntimeError('Invalid ESP DATA header')
        wanted = count*2
        deadline = time.monotonic()+6
        payload = bytearray()
        while len(payload) < wanted and time.monotonic() < deadline:
            payload.extend(self.port.read(wanted-len(payload)))
        if len(payload) != wanted: raise RuntimeError('Short ESP IQ payload')
        payload = bytes(payload)
        crc = zlib.crc32(payload) & 0xffffffff
        if crc != int(fields[2], 16): raise RuntimeError('ESP IQ CRC mismatch')
        return payload, {'crc32': '{:08x}'.format(crc), 'firmware_capture_us': int(fields[3])}


def array_bytes(value):
    return value.tobytes() if hasattr(value, 'tobytes') else value.tostring()


def decode(payload):
    values = np.frombuffer(payload, dtype=np.int8).astype(np.float32)
    # Firmware wire convention is signed (I,-Q).
    return values[0::2] - 1j*values[1::2]


def configure_gain(protocol, gain, limits):
    if gain is not None:
        lo, hi, step = limits['gain']
        if not lo <= gain <= hi or (gain-lo) % step:
            raise ValueError('ESP gain_index outside runtime limits')
    protocol.command('GAIN HARDWARE' if gain is None else 'GAIN MANUAL {}'.format(gain))


def measure(cfg):
    import serial
    target = cfg['device']
    port = routing.esp_port(target['serial'])
    if not port: raise RuntimeError('ESP receiver disconnected')
    if port != target['port']: raise RuntimeError('ESP serial device changed; retry discovery')
    link = serial.Serial(port, 2000000, timeout=.2)
    try:
        link.setRTS(False); link.setDTR(False)
        # Drain abandoned binary output without resetting/flashing the board.
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if not link.read(4096): break
        protocol = Protocol(link)
        info = protocol.command('INFO', b'S3SDR ')
        fields = info.split()
        if len(fields) < 4 or fields[1] != b'6' or fields[2] != b'burst' or int(fields[3]) < 12288:
            raise RuntimeError('Unsupported ESP-SDR profile/protocol')
        version = json.loads(protocol.command('VERSION?', b'VERSION ')[8:].decode('ascii'))
        if version.get('profile') != 'esp32s3': raise RuntimeError('Expected ESP32-S3 receiver')
        caps = protocol.command('CAPS', b'CAPS ').split()[1:]
        if any(v not in caps for v in (b'RXLIMITS', b'RX16', b'GAIN')):
            raise RuntimeError('ESP-SDR missing required capture capabilities')
        limits = json.loads(protocol.command('LIMITS?', b'LIMITS ')[7:].decode('ascii'))
        if 16000000 not in limits['rates']: raise RuntimeError('ESP-SDR 16 MS/s unavailable')
        center = cfg['frequency_hz']
        tune = int(math.floor(center/1000000.0+.5))*1000000
        protocol.command('FREQ {}'.format(tune//1000000))
        protocol.command('BANDWIDTH 13')
        gain = cfg.get('gain_index')
        configure_gain(protocol, gain, limits)
        rate = 16000000; fft_size = 4096
        sums = np.zeros(fft_size); maximum = np.zeros(fft_size)
        frames = 0; samples = 0; iq = b''; bursts = []
        started = time.time()
        deadline = time.monotonic()+cfg['dwell_seconds']
        while True:
            raw, evidence = protocol.burst()
            z = decode(raw); z -= z.mean()
            f, power = spectrum(z, rate, tune, fft_size)
            sums += power.sum(axis=0)
            maximum = np.maximum(maximum, power.max(axis=0))
            frames += power.shape[0]; samples += len(z); bursts.append(evidence)
            if cfg.get('include_iq') and not iq:
                # Preserve generic cu8 I,Q output convention; saturate -(-128).
                values = np.frombuffer(raw, dtype=np.int8).astype(np.int16)
                values[1::2] *= -1
                iq = array_bytes(np.clip(values+128, 0, 255).astype(np.uint8))
            if time.monotonic() >= deadline: break
        finished = time.time()
        protocol.command('RELEASE')
        selected = np.where((f >= center-cfg['width_hz']/2.0) & (f <= center+cfg['width_hz']/2.0))[0]
        if not len(selected): raise RuntimeError('Empty ESP spectrum window')
        avg = 10*np.log10(np.maximum(sums/frames, 1e-20))
        peak = 10*np.log10(np.maximum(maximum, 1e-20))
        bins = [[int(round(f[i])), round(float(avg[i]),2), round(float(peak[i]),2)] for i in selected]
        strongest = max(bins,key=lambda row:row[2])
        floor = float(np.median([v[1] for v in bins])); delta = strongest[2]-floor
        result = {'frequency_hz':center,'tuner_center_hz':tune,'sample_rate_hz':rate,
                  'sample_count':samples,'fft_frames':frames,'sample_seconds':float(samples)/rate,
                  'sample_started_at':started,'sample_finished_at':finished,'gain_index':gain,
                  'gain_mode':'manual' if gain is not None else 'hardware','analog_bandwidth_mhz':13,
                  'bins':bins,'bin_columns':['frequency_hz','mean_dbfs','max_hold_dbfs'],
                  'peak_hz':strongest[0],'peak_db':strongest[2],'median_db':round(floor,2),
                  'peak_above_median_db':round(delta,2),'activity_candidate':delta>=cfg['threshold_db'],
                  'level_units':'Window-normalized relative dBFS; uncalibrated, not dBm',
                  'device':target,'receive_only':True,'reception_has_gaps':True,
                  'dwell_semantics':'Wall observation budget; USB bursts have unobserved gaps',
                  'bursts':bursts,'crc_validated':True,'firmware':version,
                  'rf_note':'No intentional transmission requested; ESP PHY startup/background calibration may transmit',
                  'coverage_note':'S3 vendor characterized approximately 1.85-2.75 GHz; locally qualified at 2.442 GHz; no PLL lock indication'}
        if iq: result.update(iq_base64=base64.b64encode(iq).decode('ascii'),iq_format='cu8 interleaved I,Q',iq_bytes=len(iq))
        return result
    finally:
        # On partial binary/CRC failure, do not send ASCII into uncertain framing.
        link.close()


if __name__ == '__main__':
    print(json.dumps(measure(json.loads(sys.argv[1])), sort_keys=True))
