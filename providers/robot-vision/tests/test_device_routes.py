import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest,tempfile
from pathlib import Path
from device_routes import DeviceRoutes,resolve_url
class RegistryTests(unittest.TestCase):
 def test_rebind_persists_and_preserves_identity_and_url(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/"routes.db");r=DeviceRoutes(p)
   self.assertEqual(r.observe("cam","192.168.1.4",{"mac":"a"})["revision"],1)
   self.assertEqual(r.observe("cam","192.168.1.4")["revision"],1)
   self.assertEqual(r.observe("cam","192.168.1.31")["revision"],2)
   self.assertEqual(DeviceRoutes(p).get("cam")["identity"],{"mac":"a"})
   self.assertEqual(resolve_url("rtsp://u:p@192.168.1.4:554/mjpeg/1",dict(route_registry=p,device_id="cam")),"rtsp://u:p@192.168.1.31:554/mjpeg/1")
   with self.assertRaises(ValueError):r.observe("cam","192.168.1.2",{"mac":"b"})
   self.assertEqual(r.get("cam")["host"],"192.168.1.31")
 def test_missing_and_invalid_route(self):
  with tempfile.TemporaryDirectory() as d:
   r=DeviceRoutes(Path(d)/"routes.db")
   self.assertIsNone(r.get("absent"))
   with self.assertRaises(ValueError):r.observe("cam","not-an-ip")
   self.assertEqual(resolve_url("rtsp://old/",{}),"rtsp://old/")

 def test_http_client_uses_latest_authenticated_route(self):
  from camera_telemetry import CameraHTTPClient
  with tempfile.TemporaryDirectory() as d:
   path=str(Path(d)/"routes.db");r=DeviceRoutes(path)
   config=dict(url="rtsp://192.168.1.4:554/mjpeg/1",device_id="cam",route_registry=path)
   client=CameraHTTPClient(config)
   r.observe("cam","192.168.1.4")
   self.assertEqual(client._connection()[0],"http://192.168.1.4")
   r.observe("cam","192.168.1.31")
   self.assertEqual(client._connection()[0],"http://192.168.1.31")

 def test_neighbor_discovery_requires_exact_mac_and_unambiguous_route(self):
  from device_routes import refresh_neighbors
  with tempfile.TemporaryDirectory() as d:
   r=DeviceRoutes(Path(d)/"routes.db")
   r.observe("relay","192.168.1.17",{"mac":"30:ed:a0:d9:5f:24"})
   self.assertEqual(refresh_neighbors(r,[("192.168.1.99","30:ed:a0:d9:5f:25")]),[])
   self.assertEqual(len(refresh_neighbors(r,[("192.168.1.20","30:ed:a0:d9:5f:24")])),1)
   self.assertEqual(r.get("relay")["host"],"192.168.1.20")

 def test_gateway_route_change_provisions_once_and_recovers_lost_reply(self):
  import json,io
  from unittest.mock import patch
  from device_routes import provision_routes
  class Wire:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def connect(self,*args):pass
   def getsockname(self):return ("192.168.1.14",1234)
  with tempfile.TemporaryDirectory() as d:
   r=DeviceRoutes(Path(d)/"routes.db");r.observe("cam","192.168.1.31")
   p=Path(d)/"push.json";p.write_text(json.dumps(dict(receiver_id="cam",gateway_host="192.168.1.200",port=9878,token="x"*40)))
   status=dict(configured=True,host="192.168.1.14",port=9878)
   with patch("socket.socket",return_value=Wire()),patch("urllib.request.urlopen",side_effect=[io.BytesIO(b"{}"),io.BytesIO(json.dumps(status).encode())]) as call:
    self.assertEqual(provision_routes(r,{"cam":str(p)})[0]["state"],"configured")
    self.assertEqual(provision_routes(r,{"cam":str(p)}),[])
    self.assertEqual(call.call_count,2)
   config=json.loads(p.read_text());config["gateway_host"]="192.168.1.200";p.write_text(json.dumps(config))
   with patch("socket.socket",return_value=Wire()),patch("urllib.request.urlopen",return_value=io.BytesIO(json.dumps(status).encode())) as call:
    self.assertEqual(provision_routes(r,{"cam":str(p)})[0]["state"],"configured")
    self.assertEqual(call.call_count,1)
