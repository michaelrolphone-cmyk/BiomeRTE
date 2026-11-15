"""Publish device-clock sensors without inventing camera frames or issuing outputs."""
import json
import logging
import threading
import time
from urllib.request import Request
from camera_telemetry import CameraHTTPClient
from capture_packet import validate_packet

class SensorTickStream:
    def __init__(self, service):
        self.service=service
        self.client=CameraHTTPClient(service.config['camera'],{'network_timeout_s':2.})
        self.stop=threading.Event()
        self.thread=None
        self.last_identity=None
        self.error=None
        self.published=0
        self._push_snapshot=None

    def pushed_sensor(self,name):
        snapshot=self._push_snapshot
        if snapshot is None:return None
        packet,interval=snapshot
        value=packet.get(name)
        if value is None:return None
        # Cache consumers retain the original device-clock interval. Re-reading
        # a cached packet cannot give it a new receipt time.
        return value,interval[0],interval[1]

    def start(self):
        from sensor_push import SensorPushReceiver
        self.push=SensorPushReceiver(self)
        socket_camera = getattr(getattr(self.service, 'camera', None), '_websocket', False)
        if self.push.configured or socket_camera:
            self.service.ahrs.source.set_push_source(lambda:self.pushed_sensor('ahrs'))
            self.service.ultrasonic.set_push_source(lambda:self.pushed_sensor('ultrasonic'))
        self.push.start()
        if self.push.configured or socket_camera:
            return self
        self.thread=threading.Thread(target=self._run,name='sensor-tick-stream',daemon=True)
        self.thread.start()
        return self

    def publish(self,packet,started,received,capture_interval=None):
        validate_packet(packet,allow_sensor_tick=True)
        if packet['schema']!='biome.sensor_tick.v1':raise ValueError('Expected independent sensor tick')
        identity=(packet['boot_id'],packet['capture_monotonic_us'])
        if identity==self.last_identity:return False
        age=(packet['served_monotonic_us']-packet['capture_monotonic_us'])/1e6
        interval=[started-age,received-age] if capture_interval is None else capture_interval
        if capture_interval is not None:self._push_snapshot=(packet,interval)
        publication_started=time.monotonic()
        from datetime import datetime,timezone
        frame=dict(frame_id=packet['frame_id'],sensor_packet=packet,
            source_id=self.service.config['camera'].get('source_id','camera'),
            capture_host_monotonic_interval_s=interval,measurement_monotonic_s=sum(interval)/2,
            receive_decode_monotonic_s=received,capture_age_at_decode_upper_s=max(0.,received-interval[0]))
        events=(self.service._stream_motion_events(frame)
                if hasattr(self.service,'_stream_motion_events') else [])
        metadata=dict(frame_id=packet['frame_id'],sensor_packet=packet,sensor_tick=True,image_available=False,
            type='telemetry',camera_delivery='independent_sensor_tick',
            camera=packet.get('camera') or {},processed_monotonic=received,
            processed_monotonic_basis='sensor_packet_receive_time',
            receive_decode_timestamp=datetime.now(timezone.utc).isoformat(),
            source_timestamp=None,source_timestamp_status='sensor_tick_not_camera_exposure',
            sensor_delivery='websocket_push' if capture_interval is not None else 'http_sensor_tick',
            capture_host_monotonic_interval_s=list(interval),
            capture_age_upper_s=max(0., received-interval[0]),
            sensor_clock_uncertainty_s=(interval[1]-interval[0])/2,
            sensor_stream_sequence=packet.get('stream_sequence'),
            map_id=self.service.map_id,map_revision=self.service.revision,
            session_id=self.service.session_id,ahrs=self.service._stream_ahrs(frame),
            ultrasonic=self.service._capture_sensor_stream(frame,'ultrasonic'),
            motion_events=events,**(self.service._drive_response_telemetry()
                if hasattr(self.service,'_drive_response_telemetry') else {}))
        if getattr(self.service,'lidar',None) is not None:
            self.service.lidar.enqueue(packet,interval)
            metadata['lidar']=self.service.lidar.status()
        if capture_interval is not None and isinstance(packet.get('ftm'),dict):
            # Preserve the device capture interval for local and remote ranging solvers.
            metadata['ranging_observation']={
                'receiver_id':packet['receiver_id'],'boot_id':packet['boot_id'],
                'uptime_ms':(packet['capture_monotonic_us']//1000) & 0xffffffff,
                'capture_host_monotonic_interval_s':list(capture_interval),
                'ftm':packet['ftm'],'ftm_responder':packet.get('ftm_responder')}
            if packet.get('wifi'):
                metadata['ranging_observation']['wifi']=packet['wifi']
        metadata['sensor_enrichment_s']=time.monotonic()-publication_started
        self.service.live_frames.publish(metadata,b'')
        self.last_identity=identity;self.published+=1;self.error=None
        return True

    def _run(self):
        interval=1./float(self.service.config['camera'].get('expected_fps',5.))
        while not self.stop.is_set():
            started=time.monotonic()
            try:
                status=self.service.camera.status()
                age=status.get('last_decode_age_s')
                if not self.push.configured and (age is None or age>.6):
                    origin,headers=self.client._connection()
                    with self.client._open(Request(origin+'/api/sensors/capture',headers=headers),timeout=2.) as response:
                        if response.status!=200:raise ConnectionError('Sensor tick endpoint unavailable')
                        packet=json.load(response)
                    self.publish(packet,started,time.monotonic())
            except Exception as exc:
                self.error=type(exc).__name__+':'+str(exc)[:120]
            self.stop.wait(max(.001,interval-(time.monotonic()-started)))

    def close(self):
        self.stop.set()
        if getattr(self,"push",None) is not None:self.push.close()
        if getattr(self,'thread',None):self.thread.join(timeout=3)
