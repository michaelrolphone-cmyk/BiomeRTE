import http.client
import json
import struct
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from leased_publish import LiveFrames
from leased_streams import exact
from vision_service import handler_for

class RearLiveIsolationTests(unittest.TestCase):
    def test_front_and_rear_streams_keep_source_identity_and_share_map(self):
        service=SimpleNamespace(_stop=threading.Event(),live_frames=LiveFrames(),
            rear_live_frames=LiveFrames(),live_maps=LiveFrames())
        server=ThreadingHTTPServer(("127.0.0.1",0),handler_for(service))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        connections=[]
        try:
            responses=[]
            for path in ("/v1/live","/v1/live/rear"):
                connection=http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=2)
                connections.append(connection);connection.request("GET",path)
                response=connection.getresponse();self.assertEqual(response.status,200)
                responses.append(response)
            for name,feed in (("rear",service.rear_live_frames),("front",service.live_frames)):
                feed.publish({"frame_id":name+"-1","camera_id":name,"map_id":"ONE-SHARED-MAP",
                              "map_revision":19},name.encode())
            for name,response in zip(("front","rear"),responses):
                value=exact(response,struct.unpack("!I",exact(response,4))[0])
                size=struct.unpack("!I",value[:4])[0];metadata=json.loads(value[4:4+size])
                self.assertEqual(metadata["camera_id"],name)
                self.assertEqual(metadata["map_id"],"ONE-SHARED-MAP")
                self.assertEqual(value[4+size:],name.encode())
            self.assertIsNot(service.live_frames,service.rear_live_frames)
        finally:
            service._stop.set()
            for feed in (service.live_frames,service.rear_live_frames,service.live_maps):feed.close()
            for connection in connections:connection.close()
            server.shutdown();server.server_close();thread.join()

if __name__=="__main__":unittest.main()
