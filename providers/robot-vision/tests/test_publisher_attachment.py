"""The native publisher uses two event loops for the same vision session."""
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from leased_streams import VisionStreams, publication_grant


def grant(**changes):
    return dict(dict(id='session', authority='node', provider='camera', server='rover',
                     contract='robot.vision.stream:1', resource='vision', lease='lease',
                     epoch='epoch', generation=1, site='site', realm='site',
                     expires_at=time.time() + 60, lane='vision'), **changes)


class PublisherAttachmentTests(unittest.TestCase):
    def test_real_lane_threads_share_one_provider_attachment_and_open_both_sockets(self):
        both_waiting = threading.Event()
        release = threading.Event()
        attached = threading.Barrier(3)
        sockets_opened = []
        authority_calls = []
        binding = grant()

        class JoiningFuture(Future):
            def result(self, *args, **kwargs):
                both_waiting.set()
                return super().result(*args, **kwargs)

        class Remote:
            def call(self, operation, args):
                authority_calls.append((operation, args))
                if len(authority_calls) == 2:
                    both_waiting.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release authority')
                return dict(binding, role='provider', url='ws://localhost/session',
                            attachment_credential='scoped-credential')

        class Wire:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                pass

        def connect(url, *, additional_headers, **options):
            sockets_opened.append((url, additional_headers['Authorization']))
            attached.wait(5)
            publisher.running['session:' + url.rsplit('/', 1)[-1]][0].set()
            return Wire()

        publisher = VisionStreams(Remote(), 'unused')
        with patch('leased_streams.Future', JoiningFuture, create=True), \
                patch('websockets.asyncio.client.connect', side_effect=connect):
            publisher.update([binding])
            try:
                self.assertTrue(both_waiting.wait(5))
                release.set()
                attached.wait(5)
            finally:
                release.set()
                publisher.close()
        self.assertEqual(authority_calls, [('stream.attach', {'session': 'session', 'role': 'provider'})])
        self.assertCountEqual(sockets_opened, [
            ('ws://localhost/session/vision', 'Stream scoped-credential'),
            ('ws://localhost/session/map', 'Stream scoped-credential'),
        ])

    def test_failed_read_reaches_both_waiters_and_next_call_rechecks(self):
        joined = threading.Event()
        release = threading.Event()
        calls = []

        class JoiningFuture(Future):
            def result(self, *args, **kwargs):
                joined.set()
                return super().result(*args, **kwargs)

        class Remote:
            def call(self, *args):
                calls.append(args)
                if len(calls) == 1:
                    if not release.wait(5):
                        raise TimeoutError('unreleased test authority')
                    raise PermissionError('revoked')
                return {'revision': len(calls)}

        publisher = VisionStreams(Remote(), 'unused')
        with patch('leased_streams.Future', JoiningFuture), ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(publisher.read_attachment, grant(lane=lane)) for lane in ('vision', 'map')]
            try:
                self.assertTrue(joined.wait(5))
            finally:
                release.set()
            for result in futures:
                with self.assertRaisesRegex(PermissionError, 'revoked'):
                    result.result(timeout=5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(publisher.read_attachment(grant()), {'revision': 2})
        self.assertEqual(publisher.read_attachment(grant()), {'revision': 3})

    def test_different_bindings_do_not_share_or_block_authority_reads(self):
        for change in ({'id':'another'}, {'authority':'another'}, {'provider':'another'},
                       {'lease':'other-lease'}, {'epoch':'next'}, {'generation':2}):
            with self.subTest(change=change):
                rendezvous = threading.Barrier(2)
                class Remote:
                    def call(self, operation, args):
                        slot = rendezvous.wait(5)
                        return {'independent': slot}

                publisher = VisionStreams(Remote(), 'unused')
                with ThreadPoolExecutor(2) as workers:
                    results = [workers.submit(publisher.read_attachment, binding)
                               for binding in (grant(), grant(**change))]
                    self.assertEqual({r.result(timeout=6)['independent'] for r in results}, {0, 1})

    def test_each_lane_checks_binding_role_and_expiry_after_the_shared_read(self):
        hint = grant()
        for change in ({'lease':'another'}, {'server':'someone-else'}, {'realm':'elsewhere'},
                       {'site':'elsewhere'}, {'role':'server'}, {'expires_at':time.time() - 10}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                publication_grant(dict(hint, lane='map'), dict(hint, **change))
        self.assertEqual(publication_grant(dict(hint,lane='map'), dict(hint,role='provider'))['lane'], 'map')


if __name__ == '__main__':
    unittest.main()
