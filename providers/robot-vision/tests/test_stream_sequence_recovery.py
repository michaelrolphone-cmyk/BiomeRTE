"""Provider recovery must never reuse sequence numbers within a stream binding."""
import io
import pathlib
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from leased_streams import VisionStreams
from stream_sequences import StreamSequences
from biomerte.stream_router import Session, decode, frame


def grant(**changes):
    value = dict(id='session-a', authority='authority-a', epoch='epoch-a',
                 generation=7, lane='vision', expires_at=time.time()+3600,
                 contract='robot.vision.stream:1', direction='provider_to_server',
                 lanes=['vision', 'map'], limits={'max_message_bytes':4194304,
                 'buffer_depth':1, 'messages_per_second':60})
    value.update(changes)
    return value


class SequenceReservationTests(unittest.TestCase):
    def test_reopen_skips_unused_reserved_numbers_and_preserves_other_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'sequences.sqlite'
            first=StreamSequences(path, block_size=4)
            self.assertEqual(first.next(grant()),1)
            self.assertEqual(first.next(grant()),2)
            self.assertEqual(first.next(grant(lane='map')),1)
            restarted=StreamSequences(path, block_size=4)
            self.assertEqual(restarted.next(grant()),5)
            self.assertEqual(restarted.next(grant(lane='map')),5)

    def test_range_rollover_and_new_epoch_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            sequences=StreamSequences(pathlib.Path(directory)/'sequences.sqlite',block_size=2)
            self.assertEqual([sequences.next(grant()) for _ in range(5)],[1,2,3,4,5])
            self.assertEqual(sequences.next(grant(id='session-b',epoch='epoch-b')),1)
            self.assertEqual(sequences.next(grant()),6)

    def test_commit_failure_emits_no_sequence_and_can_retry_without_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'sequences.sqlite'
            sequences=StreamSequences(path,block_size=4)
            real_connect=sqlite3.connect
            class CommitFailure:
                def __init__(self,*args,**kwargs):self.db=real_connect(*args,**kwargs)
                def execute(self,*args):return self.db.execute(*args)
                def commit(self):raise OSError('injected durability failure')
                def rollback(self):return self.db.rollback()
                def close(self):return self.db.close()
            with patch('stream_sequences.sqlite3.connect',side_effect=CommitFailure):
                with self.assertRaises(OSError):sequences.next(grant())
            self.assertEqual(sequences.ranges,{})
            self.assertEqual(sequences.next(grant()),1)
            self.assertEqual(StreamSequences(path,block_size=4).next(grant()),5)

    def test_expired_reservations_are_pruned_without_losing_live_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'sequences.sqlite'
            sequences=StreamSequences(path,block_size=2)
            live=grant()
            self.assertEqual(sequences.next(live),1)
            with sqlite3.connect(str(path)) as db:
                db.execute('INSERT INTO stream_sequences VALUES(?,?,?)',('expired',100,time.time()-1))
            self.assertEqual(sequences.next(grant(id='new')),1)
            with sqlite3.connect(str(path)) as db:
                self.assertIsNone(db.execute("SELECT 1 FROM stream_sequences WHERE binding='expired'").fetchone())
            self.assertEqual(StreamSequences(path,block_size=2).next(live),3)

    def test_expired_grant_never_allocates_or_publishes(self):
        sequences=StreamSequences()
        with self.assertRaises(ValueError):sequences.next(grant(expires_at=time.time()-1))
        self.assertEqual(sequences.ranges,{})


class PublisherRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def publish_once(self,publisher,binding,router):
        stop=threading.Event();accepted=[];failures=[]
        metadata=dict(type='map_delta',frame_id='fresh-frame',source_timestamp=1,
                      processing_timestamp=2,processed_monotonic=time.monotonic(),
                      calibration_revision='kept',map_revision=13,
                      map_id='kept-map',revision=13)
        packet=frame(metadata,b'JPEG')
        class Response(io.BytesIO):
            status=200
        response=Response(struct.pack('!I',len(packet))+packet)
        class Local:
            def request(self,*args):pass
            def getresponse(self):return response
            def abort(self):response.close()
        class Wire:
            async def send(self,message):
                try:
                    router.check('provider',binding['lane'],message)
                    accepted.append(decode(message,4194304))
                except Exception as error:
                    failures.append(error)
                    raise
                finally:stop.set()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
        class Remote:
            def call(self,*args):
                return {'url':'ws://127.0.0.1:8751/'+binding['id'],
                        'attachment_credential':'fixture'}
        publisher.remote=Remote()
        with patch('leased_streams.LocalConnection',return_value=Local()),patch(
                'websockets.asyncio.client.connect',return_value=Wire()):
            await publisher.run(binding,stop)
        self.assertEqual(failures,[],'Router rejected a fresh recovery frame')
        self.assertEqual(len(accepted),1)
        meta,image=accepted[0]
        self.assertEqual(image,b'JPEG')
        self.assertEqual((meta['calibration_revision'],meta['map_id'],meta['map_revision']),
                         ('kept','kept-map',13))
        return meta['sequence']

    async def test_lane_thread_recreation_preserves_router_watermark(self):
        binding=grant();router=Session(binding);publisher=VisionStreams(None,'fixture')
        first=await self.publish_once(publisher,binding,router)
        # A newly created lane coroutine must retain the manager's binding cursor.
        second=await self.publish_once(publisher,binding,router)
        self.assertGreater(second,first)

    async def test_new_provider_instance_resumes_same_router_session_from_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'sequences.sqlite'
            binding=grant();router=Session(binding)
            first=await self.publish_once(VisionStreams(None,'fixture',sequence_path=path),binding,router)
            second=await self.publish_once(VisionStreams(None,'fixture',sequence_path=path),binding,router)
            self.assertGreater(second,first)

    async def test_vision_and_map_recover_independently(self):
        publisher=VisionStreams(None,'fixture');binding=grant();router=Session(binding)
        self.assertEqual(await self.publish_once(publisher,binding,router),1)
        self.assertEqual(await self.publish_once(publisher,grant(lane='map'),router),1)
        self.assertEqual(await self.publish_once(publisher,binding,router),2)


if __name__=='__main__':
    unittest.main()
