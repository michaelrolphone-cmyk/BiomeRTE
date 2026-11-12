import io,json,unittest
from capture_packet import read_capture,CaptureSequence,sensor_age_ms
class Response(io.BytesIO):
 def __init__(self,data):super().__init__(data);self.headers={'Content-Type':'multipart/mixed; boundary=biomeframe'}
def packet(t=1000000,boot='boot'):
 return dict(schema='biome.capture.v1',receiver_id='camera',boot_id=boot,frame_id=boot+':'+str(t),capture_monotonic_us=t,served_monotonic_us=t+50000,capture_clock='esp_timer',width=2,height=2)
def wire(meta,jpeg=b'JPEG\r\n--biomeframe-not-a-boundary'):
 raw=json.dumps(meta).encode();return (b'--biomeframe\r\nContent-Type: application/json\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw+b'\r\n--biomeframe\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(jpeg)).encode()+b'\r\n\r\n'+jpeg+b'\r\n--biomeframe--\r\n')
class CaptureTests(unittest.TestCase):
 def test_frame_sensor_identity_and_binary_lengths(self):
  p=packet();p['ahrs']={'receiver_id':'camera','boot_id':'boot'}
  data,jpeg=read_capture(Response(wire(p)));self.assertEqual(data,p);self.assertIn(b'--biomeframe-not',jpeg)
 def test_cross_boot_or_truncation_rejected(self):
  p=packet();p['radio']={'receiver_id':'camera','boot_id':'previous'}
  with self.assertRaises(ValueError):read_capture(Response(wire(p)))
  with self.assertRaises(ValueError):read_capture(Response(wire(packet())[:-6]))
 def test_clock_preserves_capture_intervals_and_restarts(self):
  s=CaptureSequence();self.assertEqual(s.accept(packet()),1);self.assertIsNone(s.accept(packet()));self.assertEqual(s.accept(packet(1050000)),1.05)
  self.assertGreater(s.accept(packet(10000,'new')),1.05);self.assertAlmostEqual(s.accept(packet(20000,'new'))-s.last_time,0)
 def test_uptime_wrap_and_future_measurements(self):
  self.assertEqual(sensor_age_ms(((1<<32)+10)*1000,(1<<32)-5),15)
  self.assertIsNone(sensor_age_ms(10000,11))

class SensorAssociationTests(unittest.TestCase):
 def test_ahrs_uses_frame_capture_clock_without_http_cache(self):
  from test_ahrs_source import telemetry
  from ahrs_tracking import AHRSTracking
  raw=telemetry();p=packet();p.update(receiver_id=raw['receiver_id'],boot_id=raw['boot_id'],frame_id=raw['boot_id']+':1000000',ahrs=raw)
  class NoHTTP:
   def snapshot(self,**kwargs):raise AssertionError('Capture association must not use the HTTP cache')
  model=AHRSTracking({},dict(enabled=True),source=NoHTTP());model.set_context('map','cal','session')
  frame=dict(frame_id=p['frame_id'],stream_epoch='stream',source_id='camera',calibration_id='cal',session_id='session',receive_decode_monotonic_s=999.,measurement_monotonic_s=10.,sensor_packet=p)
  result=model.evidence(frame)
  self.assertTrue(result['timing']['timing_qualified']);self.assertEqual(result['timing']['clock_qualification'],'same_device_capture_clock');self.assertTrue(result['sensors']['imu']['valid']);self.assertEqual(model._pending['candidates']['attitude']['frame_tick'],10.)
 def test_radio_age_uses_device_observation_not_receipt(self):
  from capture_packet import frame_radio_snapshot
  from radio_tracking import RadioTracking
  p=packet();p['radio']=dict(schema=1,receiver_id='camera',boot_id='boot',uptime_ms=1000,sequence=1,wifi=[dict(id='aa:bb:cc:dd:ee:ff',address='aa:bb:cc:dd:ee:ff',name='AP',age_ms=10,observed_uptime_ms=990,sample_count=1)],ble=[])
  envelope=frame_radio_snapshot(dict(sensor_packet=p,measurement_monotonic_s=10.,source_id='camera'))
  self.assertEqual(envelope['request_duration_s'],0);self.assertEqual(envelope['observations'][0]['effective_age_ms'],10)
  # Exercise the actual fingerprint grouping path without creating a model DB.
  model=object.__new__(RadioTracking);model.max_source_age=15;model.max_span=1
  groups=model._groups(envelope,10.)
  self.assertAlmostEqual(groups[0][0]['observed_at_s'],9.99)

class CameraCaptureTests(unittest.TestCase):
 def test_camera_thread_uses_capture_intervals_and_closes(self):
  import time
  from unittest.mock import patch
  import cv2,numpy as np
  from camera_source import CameraSource
  ok,jpeg=cv2.imencode('.jpg',np.zeros((2,2,3),dtype=np.uint8));self.assertTrue(ok)
  class Client:
   network_timeout_s=.1
   def __init__(self,*args):self.calls=0
   def _connection(self):return 'http://camera',{}
   def _open(self,*args,**kwargs):
    self.calls+=1;time.sleep(.02 if self.calls==1 else .005)
    r=Response(wire(packet(1000000 if self.calls==1 else 1100000),jpeg.tobytes()));r.getcode=lambda:200;return r
  with patch('camera_telemetry.CameraHTTPClient',Client):
   source=CameraSource(dict(url='rtsp://camera',synchronized_capture=True,expected_fps=10,network_timeout_s=.1))
   source.start()
   try:
    first=source.next_frame(-1,timeout=1);self.assertIsNotNone(first)
    second=source.next_frame(first.sequence,timeout=1);self.assertIsNotNone(second)
    self.assertAlmostEqual(second.metadata['measurement_monotonic_s']-first.metadata['measurement_monotonic_s'],.1,places=6)
    self.assertEqual(second.metadata['frame_id'],'boot:1100000');self.assertTrue(source.status()['capture_time_available'])
   finally:source.close()
   self.assertEqual(source.status()['state'],'disconnected')
