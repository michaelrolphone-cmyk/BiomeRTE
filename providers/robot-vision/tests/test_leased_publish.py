import json
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from leased_publish import LiveFrames
from leased_maps import LiveMap


def metadata(value):
    size=struct.unpack('!I',value[4:8])[0]
    return json.loads(value[8:8+size])


class LiveFeedTests(unittest.TestCase):
    def test_frames_are_newest_only_and_subscriptions_do_not_persist(self):
        feed=LiveFrames()
        with feed.subscribe() as inbox:
            for i in range(10000):feed.publish({'frame_id':str(i)},b'jpeg')
            self.assertEqual(1,inbox.qsize())
            self.assertEqual('9999',metadata(inbox.get())['frame_id'])
        self.assertFalse(feed.subscribers)

    def test_map_overflow_and_new_attachment_require_snapshot(self):
        feed=LiveMap()
        with feed.subscribe() as inbox:
            self.assertEqual('snapshot_required',metadata(inbox.get())['type'])
            for i in range(9):feed.publish('map',i,i+1,[])
            self.assertEqual('snapshot_required',metadata(inbox.get())['type'])
        with feed.subscribe() as inbox:
            self.assertEqual(9,metadata(inbox.get())['revision'])
