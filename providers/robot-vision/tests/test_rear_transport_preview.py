import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from rear_transport_preview import publish_image, publish_sensor


class RearPreviewTest(unittest.TestCase):
    def setUp(self):
        self.publisher=Mock()
        self.lidar=SimpleNamespace(lock=threading.Lock(),status=lambda: {'latest': {}})
        self.ingress=SimpleNamespace(
            rig=SimpleNamespace(config={'rear': {'calibration': {'calibration_id':'fixture'}}}),
            service=SimpleNamespace(map_id='map',revision=3,session_id='session',
                                    lidar=self.lidar,rear_live_frames=self.publisher))
        self.packet=dict(boot_id='boot',capture_monotonic_us=800000,frame_id='boot:800000',
                         lidar={'sensors':[{'id':'rear','observed_monotonic_us':799900}]})

    def test_sensor_times_are_preserved_during_clock_alignment(self):
        publish_sensor(self.ingress,self.packet,2.5)
        publish_sensor(self.ingress,self.packet,3.)
        self.publisher.publish.assert_called_once()
        data,body=self.publisher.publish.call_args.args
        self.assertIs(data['sensor_packet'],self.packet)
        self.assertEqual(self.lidar.raw_latest,(self.packet,None,2.5))
        self.assertEqual(data['capture_timing'],'device_ticks_only')
        self.assertFalse(data['image_available'])
        self.assertEqual(body,b'')

    def test_camera_preview_is_decoupled_from_mapping(self):
        packet=dict(self.packet,width=640,height=480)
        publish_image(self.ingress,packet,b'jpeg',4.)
        data,body=self.publisher.publish.call_args.args
        self.assertEqual(data['camera_id'],'rear')
        self.assertEqual(data['frame_id'],packet['frame_id'])
        self.assertEqual(data['processing_state'],'awaiting_capture_clock')
        self.assertEqual(body,b'jpeg')
