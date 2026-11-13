"""One bounded physical RTL-SDR observation; run in a killable child process.
Uses the Pi's installed librtlsdr and NumPy. No transmit or code replay support.
"""
import base64
import ctypes
import ctypes.util
import json
import math
import sys
import time
import numpy as np
import routing


def check(rc, name):
    if rc < 0: raise RuntimeError('{} failed: {}'.format(name,rc))


def spectrum(z, rate, tune, fft_size=1024):
    window=np.hanning(fft_size)
    normal=float((window**2).sum())*fft_size*128**2
    chunks=z.reshape((-1,fft_size))
    fft=np.fft.fftshift(np.fft.fft(chunks*window,axis=1),axes=1)
    power=(fft.real**2+fft.imag**2)/normal
    frequencies=tune+(np.arange(fft_size)-fft_size//2)*float(rate)/fft_size
    return frequencies,power


def measure(cfg):
    lib=ctypes.CDLL(ctypes.util.find_library('rtlsdr') or 'librtlsdr.so.0')
    # Select exact serial ourselves, avoiding numeric-serial/CLI-index ambiguity.
    rows=[]
    for i in range(lib.rtlsdr_get_device_count()):
        a,b,c=[ctypes.create_string_buffer(256) for unused in range(3)]
        check(lib.rtlsdr_get_device_usb_strings(i,a,b,c),'USB identity')
        rows.append((i,c.value.decode('utf8','replace')))
    target=cfg['device']
    matching=[i for i,s in rows if s==target['serial']] if target['unique_serial'] else [i for i,s in rows if i==target['index'] and s==target['serial']]
    if target.get('usb_path'):
        paths = routing.usb_rtl_paths(len(rows))
        matching = [i for i,s in rows if paths[i] == target['usb_path'] and s == target['serial']]
    if len(matching)!=1: raise RuntimeError('Receiver identity changed or is ambiguous')
    target = dict(target, index=matching[0])
    dev=ctypes.c_void_p();check(lib.rtlsdr_open(ctypes.byref(dev),matching[0]),'open')
    try:
        rate=1024000 if cfg['width_hz']<=500000 else 2400000
        center=cfg['frequency_hz']; tune=center+rate//4
        check(lib.rtlsdr_set_sample_rate(dev,rate),'sample rate')
        # Manual gain makes successive window levels comparable.
        check(lib.rtlsdr_set_tuner_gain_mode(dev,1),'manual gain')
        n=lib.rtlsdr_get_tuner_gains(dev,None)
        gains=(ctypes.c_int*max(1,n))();lib.rtlsdr_get_tuner_gains(dev,gains)
        gain=min(list(gains)[:n],key=lambda v:abs(v-cfg['gain_db']*10)) if n>0 else int(cfg['gain_db']*10)
        check(lib.rtlsdr_set_tuner_gain(dev,gain),'gain')
        rc=lib.rtlsdr_set_freq_correction(dev,cfg['ppm'])
        if rc not in (0,-2): check(rc,'ppm') # librtlsdr returns -2 for unchanged correction.
        check(lib.rtlsdr_set_center_freq(dev,tune),'tune')
        time.sleep(.05)
        check(lib.rtlsdr_reset_buffer(dev),'reset')
        count=65536;buf=(ctypes.c_ubyte*count)();actual=ctypes.c_int()
        # Discard one buffer after tuning/AGC settling.
        check(lib.rtlsdr_read_sync(dev,buf,count,ctypes.byref(actual)),'settle read')
        fft_size=1024
        sums=np.zeros(fft_size);maximum=np.zeros(fft_size);frames=0;sample_count=0;blocks=[];iq=b''
        wanted=int(math.ceil(rate*cfg['dwell_seconds']/32768))
        sample_start=time.time()
        for unused in range(wanted):
            check(lib.rtlsdr_read_sync(dev,buf,count,ctypes.byref(actual)),'sample')
            if actual.value!=count: raise RuntimeError('Short USB read: {}'.format(actual.value))
            raw=np.ctypeslib.as_array(buf).astype(np.float32)-127.5
            z=raw[0::2]+1j*raw[1::2];z=z-z.mean()
            f,power=spectrum(z,rate,tune,fft_size)
            sums+=power.sum(axis=0);maximum=np.maximum(maximum,power.max(axis=0));frames+=power.shape[0]
            sample_count+=len(z)
            blocks.append(round(float(10*np.log10(max(float((abs(z)**2).mean())/(128**2),1e-20))),2))
            if cfg.get('include_iq') and not iq: iq=bytes(buf)[:32768]
        mask=(f>=center-cfg['width_hz']/2)&(f<=center+cfg['width_hz']/2)
        avg=10*np.log10(np.maximum(sums/frames,1e-20));peak=10*np.log10(np.maximum(maximum,1e-20))
        selected=np.where(mask)[0];stride=max(1,int(math.ceil(len(selected)/512.0)));selected=selected[::stride]
        bins=[[int(round(f[i])),round(float(avg[i]),2),round(float(peak[i]),2)] for i in selected]
        if not bins: raise RuntimeError('Empty spectrum window')
        strongest=max(bins,key=lambda row:row[2]);floor=float(np.median([v[1] for v in bins]));delta=strongest[2]-floor
        result={'frequency_hz':center,'tuner_center_hz':tune,'sample_rate_hz':rate,'sample_count':sample_count,'fft_frames':frames,
                'sample_seconds':float(sample_count)/rate,'sample_started_at':sample_start,'sample_finished_at':time.time(),
                'gain_db':gain/10.0,'ppm':cfg['ppm'],'bins':bins,'bin_columns':['frequency_hz','mean_dbfs','max_hold_dbfs'],
                'peak_hz':strongest[0],'peak_db':strongest[2],'median_db':round(floor,2),'peak_above_median_db':round(delta,2),
                'activity_candidate':delta>=cfg['threshold_db'],'block_power_dbfs':blocks[:160],
                'level_units':'Window-normalized relative dBFS; uncalibrated, not dBm','device':target,'receive_only':True}
        if iq: result.update(iq_base64=base64.b64encode(iq).decode('ascii'),iq_format='cu8 interleaved I,Q',iq_bytes=len(iq))
        return result
    finally:
        lib.rtlsdr_close(dev)

if __name__=='__main__':
    print(json.dumps(measure(json.loads(sys.argv[1])),sort_keys=True))
