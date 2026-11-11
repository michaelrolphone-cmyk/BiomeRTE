from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from video_source import RecordedVideo, VideoInputError, replay_video


CAL = dict(validated=True, calibration_id='phone-lens-1', provenance='measured',
           width=640, height=480, fx=500., fy=500., cx=320., cy=240.,
           distortion=[0., 0., 0., 0., 0.])


class Frame:
    width, height, rotation = 640, 480, 0
    time_base = Fraction(1, 1000)

    def __init__(self, pts):
        self.pts = pts

    def to_ndarray(self, format):
        assert format == 'bgr24'
        return ('image', self.pts)


class Container:
    def __init__(self, frames):
        self.frames = frames
        self.stream = object()
        self.streams = SimpleNamespace(video=[self.stream])
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def decode(self, stream):
        assert stream is self.stream
        yield from self.frames


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'recording.mp4'
        self.path.write_bytes(b'video fixture')

    def source(self, frames, calibration=None):
        self.container = Container(frames)
        return RecordedVideo(self.path, calibration or CAL, opener=lambda path: self.container)

    def test_vfr_uses_media_pts_and_reproducible_frame_identity(self):
        source = self.source([Frame(4000), Frame(4033), Frame(4110)])
        frames = list(source.frames())
        self.assertEqual([x.timestamp_s for x in frames], [0., .033, .110])
        self.assertEqual([x.index for x in frames], [0, 1, 2])
        self.assertEqual(frames[1].frame_id, f'video:{source.sha256}:1')
        self.assertTrue(self.container.closed)
        self.assertEqual([x.frame_id for x in self.source([Frame(4000), Frame(4033)]).frames()],
                         [x.frame_id for x in frames[:2]])

    def test_invalid_time_fails_and_closes_decoder(self):
        for pts in ([2, 2], [3, 2], [None]):
            with self.subTest(pts=pts):
                source = self.source([Frame(x) for x in pts])
                with self.assertRaises(VideoInputError):
                    list(source.frames())
                self.assertTrue(self.container.closed)

    def test_calibration_dimension_mismatch_is_not_rescaled(self):
        frame = Frame(1)
        frame.width = 1920
        with self.assertRaisesRegex(VideoInputError, 'dimensions'):
            list(self.source([frame]).frames())
        self.assertTrue(self.container.closed)

    def test_recording_rotation_requires_calibrated_orientation(self):
        frame = Frame(1)
        frame.rotation = 90
        with self.assertRaisesRegex(VideoInputError, 'orientation'):
            list(self.source([frame]).frames())

    def test_calibration_cannot_be_inferred_from_rover_mount(self):
        for patch in ({'validated': False}, {'fx': None}, {'fx': True}, {'width': True},
                      {'distortion': [float('nan')] * 4}, {'provenance': ''}):
            with self.subTest(patch=patch), self.assertRaises(VideoInputError):
                self.source([], {**CAL, **patch})

    def test_empty_or_ambiguous_video_rejected(self):
        with self.assertRaisesRegex(VideoInputError, 'no decodable'):
            list(self.source([]).frames())
        source = self.source([Frame(0)])
        self.container.streams.video.append(object())
        with self.assertRaisesRegex(VideoInputError, 'one video stream'):
            list(source.frames())

    def test_every_frame_replayed_without_real_time_drop(self):
        class Backend:
            def __init__(self): self.calls = []
            def track(self, *args):
                self.calls.append(args)
                return {'localization': {'valid': len(self.calls) > 1}}
        backend = Backend()
        progress = []
        result = replay_video(self.source([Frame(0), Frame(10), Frame(300)]), backend,
                              progress=progress.append)
        self.assertEqual(len(backend.calls), 3)
        self.assertEqual(result['status'], 'decoded')
        self.assertEqual(result['localized_frames'], 2)
        self.assertEqual([x['frames_processed'] for x in progress], [1, 2, 3])

    def test_cancellation_closes_without_next_track(self):
        stop = False
        class Backend:
            def track(self, *args): return {'localization': {'valid': True}}
        def progress(value):
            nonlocal stop
            stop = True
        result = replay_video(self.source([Frame(0), Frame(40), Frame(80)]), Backend(),
                              cancelled=lambda: stop, progress=progress)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['frames_processed'], 1)
        self.assertTrue(self.container.closed)

    def test_backend_error_does_not_leave_video_open(self):
        class Backend:
            def track(self, *args): raise RuntimeError('native failure')
        with self.assertRaisesRegex(RuntimeError, 'native failure'):
            replay_video(self.source([Frame(0), Frame(40)]), Backend())
        self.assertTrue(self.container.closed)


if __name__ == '__main__':
    unittest.main()
