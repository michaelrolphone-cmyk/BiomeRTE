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

    def start(self):
        self.thread=threading.Thread(target=self._run,name='sensor-tick-stream',daemon=True)
        self.thread.start()
        return self

    def publish(self,packet,started,received):
        validate_packet(packet,allow_sensor_tick=True)
        if packet['schema']!='biome.sensor_tick.v1':raise ValueError('Expected independent sensor tick')
        identity=(packet['boot_id'],packet['capture_monotonic_us'])
        if identity==self.last_identity:return False
        age=(packet['served_monotonic_us']-packet['capture_monotonic_us'])/1e6
        interval=[started-age,received-age]
        from datetime import datetime,timezone
        frame=dict(frame_id=packet['frame_id'],sensor_packet=packet,
            source_id=self.service.config['camera'].get('source_id','camera'),
            capture_host_monotonic_interval_s=interval,measurement_monotonic_s=sum(interval)/2,
            receive_decode_monotonic_s=received,capture_age_at_decode_upper_s=age+received-started)
        events=self.service._stream_motion_events(frame)
        metadata=dict(frame_id=packet['frame_id'],sensor_tick=True,image_available=False,
            type='telemetry',camera_delivery='independent_sensor_tick',
            camera=packet.get('camera') or {},processed_monotonic=received,
            processed_monotonic_basis='sensor_packet_receive_time',
            receive_decode_timestamp=datetime.now(timezone.utc).isoformat(),
            source_timestamp=None,source_timestamp_status='sensor_tick_not_camera_exposure',
            map_id=self.service.map_id,map_revision=self.service.revision,
            session_id=self.service.session_id,ahrs=self.service._stream_ahrs(frame),
            radio=self.service._stream_radio(frame),
            ultrasonic=self.service._capture_sensor_stream(frame,'ultrasonic'),
            motion_events=events,**self.service._drive_response_telemetry())
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
                if age is None or age>.6:
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
        if self.thread:self.thread.join(timeout=3)
