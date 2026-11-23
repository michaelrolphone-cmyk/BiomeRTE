"""Real WebSocket regressions for rear fragment consumption and idle recovery."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rear_transport_preview import messages_with_idle_recovery
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

async def buffered_trial(reader):
 # Buffer real WebSocket fragments before measuring; no network delay is charged.
 # A bounded competing callback models work scheduled between consumer turns.
 loop=asyncio.get_running_loop();connected=loop.create_future();release=asyncio.Event();outcome=loop.create_future()
 count=4;payload=b"z"*23532;pieces=[payload[i:i+1400] for i in range(0,len(payload),1400)]
 async def receive(socket):
  connected.set_result(socket)
  await release.wait()
  stream=reader(socket,2)
  try:
   started=time.perf_counter()
   values=[]
   for unused in range(count):
    values.append(await anext(stream))
    await asyncio.sleep(0)
   outcome.set_result({"elapsed_s":time.perf_counter()-started,"messages":len(values),"bytes":sum(map(len,values)),"identical":values==[payload]*count})
  except BaseException as error:
   if not outcome.done():outcome.set_exception(error)
  finally:await stream.aclose()
 async with serve(receive,"127.0.0.1",0,compression=None,max_queue=None,ping_interval=None) as server:
  async with connect("ws://127.0.0.1:"+str(server.sockets[0].getsockname()[1]),compression=None,ping_interval=None) as client:
   socket=await connected
   for unused in range(count):await client.send(pieces)
   expected=count*(len(pieces)+1)
   async with asyncio.timeout(3):
    while len(socket.recv_messages.frames)<expected:await asyncio.sleep(.001)
   buffered=len(socket.recv_messages.frames)
   busy={"active":True,"turns":0}
   def contend():
    if not busy["active"]:return
    until=time.perf_counter()+.008
    while time.perf_counter()<until:pass
    busy["turns"]+=1
    if busy["turns"]<500:loop.call_soon(contend)
   loop.call_soon(contend);release.set()
   try:
    async with asyncio.timeout(8):result=await outcome
   finally:busy["active"]=False
   result.update(buffered_fragments=buffered,fragment_bytes=1400,frame_bytes=len(payload),competing_callback_ms=8,competing_callback_turns=busy["turns"],frame_budget_s=.2,within_five_fps_budget=result["elapsed_s"]<=count*.2)
   return result


class RearFragmentConsumptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffered_fragmented_messages_keep_the_five_fps_budget_under_contention(self):
        result = await buffered_trial(messages_with_idle_recovery)
        self.assertTrue(result["identical"], result)
        self.assertTrue(result["within_five_fps_budget"], result)

    async def test_empty_text_binary_and_fragmented_messages_preserve_opcode_and_bytes(self):
        loop = asyncio.get_running_loop()
        outcome = loop.create_future()
        expected = [b"", "", bytes(range(256)) * 92, "clock-caf\u00e9-reply"]
        async def receive(socket):
            try:
                outcome.set_result([value async for value in messages_with_idle_recovery(socket, 1)])
            except BaseException as error:
                if not outcome.done():
                    outcome.set_exception(error)
        async with serve(receive, "127.0.0.1", 0, compression=None) as server:
            async with connect("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]),
                               compression=None) as socket:
                await socket.send(b"")
                await socket.send("")
                payload = expected[2]
                await socket.send([payload[i:i+1400] for i in range(0, len(payload), 1400)])
                await socket.send(["clock-", "caf\u00e9", "-reply"])
            self.assertEqual(await asyncio.wait_for(outcome, 2), expected)

    async def test_stalled_partial_message_aborts_without_publishing_a_prefix(self):
        loop = asyncio.get_running_loop()
        outcome = loop.create_future()
        release = asyncio.Event()
        received = []
        async def receive(socket):
            try:
                async for value in messages_with_idle_recovery(socket, 0.08):
                    received.append(value)
            except ConnectionError as error:
                outcome.set_result((str(error), socket.transport.is_closing()))
        async def fragments():
            yield b"first-incomplete-fragment"
            await release.wait()
            yield b"must-not-complete"
        async with serve(receive, "127.0.0.1", 0) as server:
            async with connect("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])) as socket:
                sending = asyncio.create_task(socket.send(fragments()))
                try:
                    message, aborted = await asyncio.wait_for(outcome, 2)
                    self.assertEqual(message, "rear device stopped providing messages")
                    self.assertTrue(aborted)
                    await asyncio.wait_for(socket.wait_closed(), 2)
                finally:
                    release.set()
                    await asyncio.gather(sending, return_exceptions=True)
        self.assertEqual(received, [])

    async def test_external_cancellation_cleans_pending_reader_without_idle_abort(self):
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        outcome = loop.create_future()
        async def receive(socket):
            stream = messages_with_idle_recovery(socket, 1)
            reading = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            reading.cancel()
            try:
                await reading
            except asyncio.CancelledError:
                pass
            else:
                outcome.set_exception(AssertionError("Cancellation was swallowed"))
                return
            await stream.aclose()
            replacement = messages_with_idle_recovery(socket, 1)
            ready.set()
            try:
                outcome.set_result(await anext(replacement))
            except BaseException as error:
                if not outcome.done():
                    outcome.set_exception(error)
            finally:
                await replacement.aclose()
        async with serve(receive, "127.0.0.1", 0) as server:
            async with connect("ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])) as socket:
                await asyncio.wait_for(ready.wait(), 2)
                await socket.send("reader-still-usable")
                self.assertEqual(await asyncio.wait_for(outcome, 2), "reader-still-usable")


def compare_baseline(path, assert_baseline_slow=False):
    import websockets
    baseline = Path(path)
    spec = importlib.util.spec_from_file_location("retained_fragment_reader", baseline)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = Path(__file__).resolve().parents[1] / "rear_transport_preview.py"
    rows = []
    for name, reader, source in (
            ("baseline", module.messages_with_idle_recovery, baseline),
            ("candidate", messages_with_idle_recovery, current)):
        result = asyncio.run(buffered_trial(reader))
        result.update(name=name, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                      python=sys.version, websockets=websockets.__version__)
        rows.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)
    if not all(row["identical"] for row in rows) or not rows[1]["within_five_fps_budget"]:
        raise SystemExit("Candidate did not preserve bytes within the unchanged frame budget")
    if assert_baseline_slow and rows[0]["within_five_fps_budget"]:
        raise SystemExit("The requested old-fail negative control was not reproduced")


if __name__ == "__main__":
    if "--compare-baseline" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--compare-baseline", required=True, type=Path)
        parser.add_argument("--assert-baseline-slow", action="store_true")
        args = parser.parse_args()
        compare_baseline(args.compare_baseline, args.assert_baseline_slow)
    else:
        unittest.main()
