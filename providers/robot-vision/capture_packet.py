"""Camera image and sensors sharing the ESP32 capture clock, never receipt time."""
import json
import math
import re

UINT32=(1<<32)-1

def validate_packet(packet,allow_sensor_tick=False):
    if not isinstance(packet,dict) or packet.get('schema') not in (('biome.capture.v1','biome.sensor_tick.v1') if allow_sensor_tick else ('biome.capture.v1',)):
        raise ValueError('Unknown capture packet schema')
    for name in ('receiver_id','boot_id','frame_id'):
        if not isinstance(packet.get(name),str) or not packet[name]:
            raise ValueError('Missing capture identity')
    t=packet.get('capture_monotonic_us')
    if type(t) is not int or t<0 or packet.get('capture_clock')!='esp_timer':
        raise ValueError('Invalid capture clock')
    if packet['frame_id']!=packet['boot_id']+':'+str(t):raise ValueError('Capture frame identity does not match its timestamp')
    served=packet.get('served_monotonic_us')
    if type(served) is not int or served<t:raise ValueError('Invalid capture service clock')
    if packet['schema']=='biome.sensor_tick.v1':
        if packet.get('image_available') is not False or packet.get('capture_timestamp_semantics')!='sensor_snapshot_tick_no_camera_frame':raise ValueError('Sensor tick cannot claim a camera image')
    for name in (() if packet['schema']=='biome.sensor_tick.v1' else ('width','height')):
        if type(packet.get(name)) is not int or not 1<=packet[name]<=16384:
            raise ValueError('Invalid frame dimensions')
    for name in ('ahrs','radio','ultrasonic'):
        data=packet.get(name)
        if data is None:continue
        if not isinstance(data,dict) or any(data.get(k)!=packet[k] for k in ('receiver_id','boot_id')):
            raise ValueError('Sensor and camera belong to different devices or boots')
    if packet.get("ultrasonic") is not None:
        from ultrasonic_source import validate_ultrasonic
        validate_ultrasonic(packet["ultrasonic"])
    return packet


def sensor_age_ms(capture_us,observed_uptime_ms):
    if type(observed_uptime_ms) is not int or not 0<=observed_uptime_ms<=UINT32:
        return None
    age=((capture_us//1000)-observed_uptime_ms)&UINT32
    return age if age<(1<<31) else None


def read_capture(response,max_jpeg_bytes=16*1024*1024):
    """Read one length-delimited metadata/JPEG bundle; never scan JPEG bytes for boundaries."""
    ctype=response.headers.get('Content-Type','')
    match=re.fullmatch(r'multipart/mixed;\s*boundary=([A-Za-z0-9_-]{1,70})',ctype)
    if not match:raise ValueError('Capture endpoint did not return a synchronized frame')
    boundary=('--'+match.group(1)).encode()
    if response.readline(128).strip()!=boundary:raise ValueError('Missing capture boundary')
    parts=[]
    for index in range(2):
        headers={};size=0
        while True:
            line=response.readline(1025);size+=len(line)
            if not line or len(line)>1024 or size>8192:raise ValueError('Invalid capture headers')
            if line in (b'\r\n',b'\n'):break
            key,sep,value=line.decode('ascii').partition(':')
            key=key.strip().lower()
            if not sep or key in headers:raise ValueError('Invalid capture header')
            headers[key]=value.strip()
        n=int(headers.get('content-length','-1'))
        maximum=131072 if index==0 else max_jpeg_bytes
        if not 0<n<=maximum:raise ValueError('Invalid capture part size')
        chunks=[];left=n
        while left:
            data=response.read(left)
            if not data:raise ValueError('Truncated capture packet')
            chunks.append(data);left-=len(data)
        parts.append((headers,b''.join(chunks)))
        if response.readline(3)!=b'\r\n':raise ValueError('Invalid capture separator')
        end=response.readline(128).strip()
        if end!=boundary+(b'--' if index else b''):raise ValueError('Invalid capture boundary')
    if parts[0][0].get('content-type')!='application/json' or parts[1][0].get('content-type')!='image/jpeg':
        raise ValueError('Invalid capture part types')
    packet=validate_packet(json.loads(parts[0][1]))
    return packet,parts[1][1]


class CaptureSequence:
    """Keep native tracking time monotonic while preserving exact source intervals."""
    def __init__(self):self.boot=None;self.last_us=None;self.offset=0.;self.last_time=None
    def accept(self,packet,anchor_s=None):
        key=(packet['receiver_id'],packet['boot_id']);t=packet['capture_monotonic_us']
        if key==self.boot and self.last_us is not None and t<=self.last_us:return None
        if key!=self.boot:
            target=t/1e6 if anchor_s is None else float(anchor_s)
            if self.last_time is not None:target=max(target,self.last_time+1e-6)
            self.offset=target-t/1e6
            self.boot=key
        self.last_us=t;self.last_time=t/1e6+self.offset
        return self.last_time


def frame_ahrs_snapshot(frame):
    from ahrs_source import validate_ahrs_snapshot, _sensor, _calibration
    packet=validate_packet(frame['sensor_packet'],allow_sensor_tick=True)
    raw=packet.get('ahrs')
    if raw is None:return None
    validate_ahrs_snapshot(raw)
    tick=frame['measurement_monotonic_s']
    sensors={name:_sensor(frame.get('source_id','camera'),raw,name,tick,0.)
             for name in ('imu','attitude','mag','barometer')}
    return dict(source_id=frame.get('source_id'),receiver_id=raw['receiver_id'],boot_id=raw['boot_id'],
                received_monotonic=tick,received_at=None,request_duration_s=0.,
                telemetry=raw,sensors=sensors,calibration=_calibration(raw['ahrs']),
                available=True,device_frame_aligned=packet['schema']=='biome.capture.v1',sensor_tick_aligned=packet['schema']=='biome.sensor_tick.v1',
                capture_monotonic_us=packet['capture_monotonic_us'])


def frame_radio_snapshot(frame):
    from camera_telemetry import observation_id,validate_telemetry_snapshot
    packet=validate_packet(frame['sensor_packet'],allow_sensor_tick=True);raw=packet.get('radio')
    if raw is None:return None
    validate_telemetry_snapshot(raw)
    observations=[]
    for radio in ('wifi','ble'):
        for index,row in enumerate(raw.get(radio,[])):
            age=sensor_age_ms(packet['capture_monotonic_us'],row.get('observed_uptime_ms'))
            if age is None:continue
            observations.append(dict(radio=radio,index=index,effective_age_ms=age,
                source_observation_id=observation_id(frame.get('source_id','camera'),raw,radio,row)))
    return dict(source_id=frame.get('source_id'),received_monotonic=frame['measurement_monotonic_s'],
                received_at=None,request_duration_s=0.,telemetry=raw,observations=observations,
                device_frame_aligned=packet['schema']=='biome.capture.v1',sensor_tick_aligned=packet['schema']=='biome.sensor_tick.v1',fresh=True)
