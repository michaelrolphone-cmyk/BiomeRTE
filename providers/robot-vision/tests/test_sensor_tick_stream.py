import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from capture_packet import validate_packet
from sensor_tick_stream import SensorTickStream

def packet(t=1000000):
    return dict(schema='biome.sensor_tick.v1',receiver_id='cam',boot_id='boot',
        frame_id='boot:'+str(t),capture_monotonic_us=t,served_monotonic_us=t+50000,
        capture_clock='esp_timer',image_available=False,
        capture_timestamp_semantics='sensor_snapshot_tick_no_camera_frame',
        camera=dict(available=False,init_error='ESP_ERR_NOT_FOUND'))

class SensorTicks(unittest.TestCase):
    def test_explicit_sensor_schema_cannot_pass_image_parser(self):
        p=packet()
        with self.assertRaises(ValueError):validate_packet(p)
        self.assertIs(validate_packet(p,allow_sensor_tick=True),p)
        for changes in [dict(image_available=True),dict(frame_id='other'),dict(served_monotonic_us=999999),
                        dict(capture_timestamp_semantics='camera_exposure'),dict(ahrs=dict(receiver_id='cam',boot_id='old'))]:
            with self.assertRaises(ValueError):validate_packet(dict(p,**changes),allow_sensor_tick=True)

    def test_publishes_sensor_clock_and_events_without_image_or_outputs(self):
        service=SimpleNamespace(config=dict(camera=dict(url='http://camera',source_id='cam')),
            map_id='map',revision=3,session_id='session',live_frames=Mock(),
            _stream_motion_events=Mock(return_value=dict(events=[dict(event_id='turn',kind='turning')])),
            _stream_ahrs=Mock(return_value=dict(valid=True)),_stream_radio=Mock(return_value={}),
            _capture_sensor_stream=Mock(return_value={}),_drive_response_telemetry=Mock(return_value={}))
        stream=SensorTickStream(service)
        self.assertTrue(stream.publish(packet(),10.,10.1))
        metadata,pixels=service.live_frames.publish.call_args.args
        self.assertEqual(pixels,b'')
        self.assertEqual(metadata['type'],'telemetry')
        self.assertFalse(metadata['image_available'])
        self.assertEqual(metadata['source_timestamp_status'],'sensor_tick_not_camera_exposure')
        self.assertEqual(metadata['motion_events']['events'][0]['event_id'],'turn')
        frame=service._stream_ahrs.call_args.args[0]
        self.assertAlmostEqual(frame['measurement_monotonic_s'],10.)
        self.assertEqual(frame['capture_host_monotonic_interval_s'],[9.95,10.049999999999999])
        self.assertFalse(stream.publish(packet(),10.2,10.3))
        self.assertEqual(service.live_frames.publish.call_count,1)
        self.assertTrue(stream.publish(packet(1200000),10.2,10.3))

if __name__=='__main__':unittest.main()
