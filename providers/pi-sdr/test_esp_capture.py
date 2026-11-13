import unittest
import zlib
from unittest import mock
import numpy as np
from esp_capture import Protocol, decode, array_bytes
from capture import spectrum


class Port:
    def __init__(self, response):
        self.response=response;self.writes=[]
    def read(self,n):
        value=self.response[:n];self.response=self.response[n:];return value
    def write(self,value): self.writes.append(value)


class ProtocolTests(unittest.TestCase):
    def test_stale_binary_prefix_resynchronization(self):
        port=Port(b'\xff\x00oldtailS3SDR 6 burst 16380\n')
        self.assertEqual(b'S3SDR 6 burst 16380',Protocol(port).command('INFO',b'S3SDR '))
        self.assertEqual([b'INFO\n'],port.writes)

    def test_crc_payload_consumed_exactly(self):
        raw=bytes(bytearray(range(16)))
        port=Port(('DATA 8 {:08x} 42\n'.format(zlib.crc32(raw)&0xffffffff)).encode()+raw+b'OK\n')
        payload,receipt=Protocol(port).burst(8)
        self.assertEqual(raw,payload);self.assertEqual(b'OK\n',port.response)
        self.assertEqual(42,receipt['firmware_capture_us'])

    def test_crc_rejection_sends_no_followup(self):
        port=Port(b'DATA 2 00000000 42\n'+bytes(bytearray([1,2,3,4])))
        with self.assertRaises(RuntimeError):Protocol(port).burst(2)
        self.assertEqual([b'CAP16 2 6\n'],port.writes)

    def test_short_payload_rejected_without_ascii_followup(self):
        port=Port(b'\\x01\\x02')
        protocol=Protocol(port)
        protocol.command=lambda *args:b'DATA 2 00000000 42'
        with mock.patch('esp_capture.time.monotonic',side_effect=[0,0,7]):
            with self.assertRaises(RuntimeError):protocol.burst(2)
        self.assertEqual([],port.writes)

    def test_iq_sign_and_tone_frequency(self):
        size=4096;rate=16000000;tune=2442000000
        tone=60*np.exp(2j*np.pi*32*np.arange(size)/size)
        wire=np.empty(size*2,dtype=np.int8)
        wire[0::2]=np.round(tone.real);wire[1::2]=np.round(-tone.imag)
        z=decode(array_bytes(wire))
        self.assertLess(float(abs(z-tone).max()),1)
        f,p=spectrum(z,rate,tune,size)
        self.assertEqual(tune+125000,int(f[np.argmax(p[0])]))


if __name__=='__main__':unittest.main()
