"""An idle local feed is valid; lease loss must still interrupt its reader."""
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from leased_streams import LocalConnection, VisionStreams, exact


class IdleFeedTests(unittest.TestCase):
    def test_idle_http_body_has_no_command_timeout_and_shutdown_interrupts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(pathlib.Path(directory)/'feed.sock')
            server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            server.bind(path);server.listen(1)
            release=threading.Event()
            def serve():
                peer,_=server.accept()
                try:
                    peer.recv(4096)
                    peer.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n')
                    release.wait(5)
                finally:peer.close()
            serving=threading.Thread(target=serve);serving.start()
            connection=LocalConnection(path)
            response=None
            try:
                self.assertIsNone(connection.timeout)
                connection.request('GET','/v1/live/map')
                response=connection.getresponse()
                self.assertEqual(response.status,200)
                self.assertIsNone(connection.stream_socket.gettimeout())
                self.assertIsNone(connection.sock)  # HTTP/1.0 response owns it.
                entered=threading.Event();finished=threading.Event()
                def read():
                    entered.set()
                    try:exact(response,4)
                    except (EOFError,OSError):pass
                    finally:finished.set()
                reader=threading.Thread(target=read);reader.start()
                entered.wait(1)
                self.assertFalse(finished.wait(.05))
                streams=VisionStreams(None,path)
                stop=threading.Event()
                streams.running['session:map']=(stop,reader)
                streams.connections['session:map']=connection
                start=time.monotonic()
                streams.close()
                self.assertLess(time.monotonic()-start,1)
                self.assertTrue(stop.is_set())
                self.assertTrue(finished.is_set())
                self.assertFalse(reader.is_alive())
            finally:
                connection.abort()
                if response:response.close()
                release.set();serving.join(2);server.close()
