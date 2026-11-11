import asyncio
import base64
import hashlib
import hmac
import json
import unittest
from biomerte.stream_router import Session, Peer, credential, frame
from biomerte.store import Fault, encode


def grant():
    return dict(id='session', epoch='epoch', authority='controller-one', generation=4,
        limits=dict(max_message_bytes=4096, buffer_depth=1, messages_per_second=60),
        direction='bidirectional', delivery='supersedable', lanes=['vision','control','map'],
        role='server', expires_at=200, credential_expires_at=150)


class StreamProtocol(unittest.TestCase):
    def meta(self, **extra):
        return dict(authority='controller-one', generation=4, epoch='epoch', sequence=1, **extra)

    def test_signed_credential_expiry_and_authority_binding(self):
        raw = encode(grant()).encode()
        token = base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hmac.new(b'secret',raw,hashlib.sha256).hexdigest()
        self.assertEqual('epoch', credential(token, {'controller-one':'secret'}, now=100)['epoch'])
        for keys, now in (({'controller-one':'wrong'},100), ({'controller-one':'secret'},151), ({'controller-two':'secret'},100)):
            with self.assertRaises(Fault):
                credential(token, keys, now=now)

    def test_control_freshness_duplicates_epoch_and_controller(self):
        session=Session(grant())
        msg=self.meta(type='desired', outputs={'forward':True}, issued_at=99.8, expires_at=100.5)
        session.check('server','control',json.dumps(msg),now=100)
        with self.assertRaises(Fault):
            session.check('server','control',json.dumps(msg),now=100)
        for change in ({'epoch':'old'}, {'authority':'controller-two'}, {'generation':3},
                       {'issued_at':98.,'expires_at':99.}, {'type':'ping'}):
            other=dict(msg, sequence=2, **change)
            with self.assertRaises(Fault):
                session.check('server','control',json.dumps(other),now=100)

    def test_vision_identity_and_contract_lane_separation(self):
        session=Session(grant())
        metadata=self.meta(frame_id='f1',source_timestamp=1,processing_timestamp=2,calibration_revision=1,map_revision=3)
        session.check('provider','vision',frame(metadata,b'image'),now=100)
        g=grant()
        g['lanes']=['vision']
        with self.assertRaises(Fault):
            Session(g).check('server','control',json.dumps(self.meta()),now=100)

    def test_slow_video_queue_retains_only_newest(self):
        class Socket:
            async def close(self, *args): pass
        p=Peer(Socket(),1,'supersedable')
        for i in range(10000):
            p.put(i)
        self.assertEqual(1,p.queue.qsize())
        self.assertEqual(9999,p.queue.get_nowait())
        ordered=Peer(Socket(),1,'ordered')
        ordered.put('first')
        with self.assertRaises(Fault):
            ordered.put('second')

    def test_oversized_binary_and_invalid_metadata_are_rejected(self):
        with self.assertRaises(Fault):
            Session(grant()).check('provider','vision',b'x'*5000,now=100)
