import asyncio
import unittest

from rear_transport_preview import messages_with_idle_recovery
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


class RearFragmentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_transfer_with_steady_progress_then_idle_disconnect(self):
        quiet = 0.25
        received = []
        outcome = asyncio.get_running_loop().create_future()

        async def receive(socket):
            try:
                async for message in messages_with_idle_recovery(socket, quiet):
                    received.append(message)
            except ConnectionError as error:
                outcome.set_result(str(error))

        async def jpeg():
            for index in range(7):
                await asyncio.sleep(quiet / 4)
                yield bytes([index])

        async with serve(receive, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as socket:
                try:
                    await socket.send(jpeg())
                    await socket.send(["clock", "-reply"])
                except ConnectionClosed:
                    pass
                self.assertEqual(await asyncio.wait_for(outcome, 2),
                                 "rear device stopped providing messages")
        self.assertEqual(received, [bytes(range(7)), "clock-reply"])

    async def test_clean_disconnect_between_messages(self):
        outcome = asyncio.get_running_loop().create_future()

        async def receive(socket):
            outcome.set_result([m async for m in messages_with_idle_recovery(socket, 1)])

        async with serve(receive, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as socket:
                await socket.send(b"")
        self.assertEqual(await asyncio.wait_for(outcome, 2), [b""])
