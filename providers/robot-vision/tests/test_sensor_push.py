import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from sensor_push import ClockAlignment, SensorPushReceiver


class ClockTests(unittest.TestCase):
    def test_start_wires_tracking_source_and_sonar_cache(self):
        from ahrs_source import AHRSTelemetrySource
        from camera_telemetry import SensorTelemetrySource
        from sensor_tick_stream import SensorTickStream
        ahrs=AHRSTelemetrySource({},dict(enabled=True),client=object())
        sonar=SensorTelemetrySource({},dict(enabled=True),client=object())
        stream=object.__new__(SensorTickStream)
        stream.service=types.SimpleNamespace(ahrs=types.SimpleNamespace(source=ahrs),ultrasonic=sonar)
        stream._push_snapshot=None;stream._run=lambda:None;stream.stop=threading.Event()
        receiver=types.SimpleNamespace(configured=True,start=lambda:None,close=lambda:None)
        with patch('sensor_push.SensorPushReceiver',return_value=receiver):
            stream.start();stream.close()
        self.assertIsNotNone(ahrs._push_reader)
        self.assertIsNotNone(sonar._push_reader)
        self.assertIsNone(ahrs._push_reader())

    def test_push_cache_replaces_http_without_refreshing_old_data(self):
        from ahrs_source import AHRSTelemetrySource
        from camera_telemetry import SensorTelemetrySource
        client=types.SimpleNamespace(get_ahrs=lambda:(_ for _ in ()).throw(AssertionError('HTTP should not run')))
        raw=dict(schema=1,type='ahrs',receiver_id='rover',boot_id='boot',uptime_ms=1000,sequence=1,
                 ahrs=dict(worker_ready=True,paused=False))
        source=AHRSTelemetrySource({},dict(enabled=True),client=client,monotonic=lambda:10.)
        source.set_push_source(lambda:(raw,8.9,9.))
        self.assertTrue(source.poll_once())
        self.assertGreaterEqual(source.snapshot()['elapsed_since_receive_s'],1.)
        source.set_push_source(lambda:None)
        self.assertFalse(source.poll_once())
        generic=SensorTelemetrySource({},dict(enabled=True),client=object(),monotonic=lambda:10.)
        generic._read_snapshot=lambda:(_ for _ in ()).throw(AssertionError('HTTP should not run'))
        generic._observations=lambda value,elapsed:[]
        generic.set_push_source(lambda:(dict(receiver_id='rover',boot_id='boot',uptime_ms=1000),8.9,9.))
        self.assertTrue(generic.poll_once())
        self.assertEqual(generic.snapshot()['elapsed_since_receive_s'],1.)

    def test_publisher_does_not_require_retired_radio_stream(self):
        module=types.ModuleType('camera_telemetry');module.CameraHTTPClient=object
        packet_module=types.ModuleType('capture_packet');packet_module.validate_packet=lambda p,**kw:p
        with patch.dict('sys.modules',{'camera_telemetry':module,'capture_packet':packet_module}):
            import sensor_tick_stream
            service=types.SimpleNamespace(config={'camera':{}},map_id='map',revision=1,session_id='session',
                _stream_ahrs=lambda f:{},_capture_sensor_stream=lambda f,n:{},
                live_frames=types.SimpleNamespace(publish=lambda m,b:published.append((m,b))))
            stream=object.__new__(sensor_tick_stream.SensorTickStream)
            stream.service=service;stream.last_identity=None;stream.published=0
            published=[]
            stream.publish(dict(schema='biome.sensor_tick.v1',receiver_id='rover',boot_id='boot',
                frame_id='boot:1000000',capture_monotonic_us=1000000,served_monotonic_us=1000001),
                10.,10.01,capture_interval=[10.,10.005])
            self.assertEqual(published[0][0]['sensor_delivery'],'websocket_push')
            self.assertEqual(published[0][1],b'')

    def test_exchange_bounds_real_capture_and_preserves_delay(self):
        clock=ClockAlignment()
        clock.observe('boot',10.,10.04,1000000,1001000)
        interval=clock.capture_interval(dict(boot_id='boot',capture_monotonic_us=1100000),10.3)
        self.assertLessEqual(interval[0],10.12)
        self.assertGreaterEqual(interval[1],10.12)
        self.assertGreater(10.3-interval[0],.19)
        with self.assertRaises(ValueError):
            clock.capture_interval(dict(boot_id='new-boot',capture_monotonic_us=1100000),10.3)

    def test_invalid_clock_cannot_make_data_fresh(self):
        clock=ClockAlignment()
        with self.assertRaises(ValueError):
            clock.observe('boot',10.,9.,1000000,1001000)

    def test_slow_exchange_does_not_discard_a_tighter_measured_clock_bound(self):
        clock=ClockAlignment()
        clock.observe('boot',10.,10.04,1000000,1001000)
        clock.observe('boot',11.,13.,2000000,2001000)
        self.assertLess(clock.bounds[1]-clock.bounds[0],.05)
        clock.observe('other-boot',11.,13.,2000000,2001000)
        self.assertGreater(clock.bounds[1]-clock.bounds[0],1.)


class PushTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_push_delivers_samples_and_ack_without_camera_polling(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        import socket
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
            token='test-token-'+'a'*40
            Path(directory,'sensor-push.json').write_text(json.dumps(dict(listen='127.0.0.1',port=port,token=token,receiver_id='rover')))
            observed=[]
            stream=types.SimpleNamespace(service=types.SimpleNamespace(state_dir=Path(directory)),
                publish=lambda *a,**kw: observed.append((a,kw)),error=None)
            receiver=SensorPushReceiver(stream)
            # Packet validation is separately covered by capture_packet tests;
            # this fixture isolates authentication, clocks and push/ack transport.
            module=types.ModuleType('capture_packet');module.validate_packet=lambda p,**kw:p
            with patch.dict('sys.modules',{'capture_packet':module}):
                task=asyncio.create_task(receiver.serve())
                await asyncio.sleep(.05)
                try:
                    with self.assertRaises(InvalidStatus):
                        async with connect(f'ws://127.0.0.1:{port}/sensors'):pass
                    async with connect(f'ws://127.0.0.1:{port}/sensors',additional_headers={'Authorization':'Sensor '+token}) as ws:
                        clock=json.loads(await ws.recv())
                        # The device may reply after the next synchronization
                        # interval. It still answers the same outstanding ID.
                        await asyncio.sleep(1.1)
                        await ws.send(json.dumps(dict(type='clock',id=clock['id'],boot_id='boot',received_monotonic_us=1000000,served_monotonic_us=1001000)))
                        p=dict(schema='biome.sensor_tick.v1',receiver_id='rover',boot_id='boot',capture_monotonic_us=1001000,
                               stream_sequence=1,ahrs=dict(ahrs=dict(imu_samples=[dict(monotonic_us=1000000)])))
                        await ws.send(json.dumps(p))
                        ack=json.loads(await ws.recv())
                        self.assertEqual(ack['monotonic_us'],1000000)
                        self.assertEqual(ack['stream_sequence'],1)
                        self.assertEqual(len(observed),1)
                        self.assertIn('capture_interval',observed[0][1])
                finally:
                    receiver.stop.set();await task


if __name__=='__main__':unittest.main()
