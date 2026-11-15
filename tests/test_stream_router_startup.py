import asyncio,unittest
import sys
from types import SimpleNamespace,ModuleType
from unittest.mock import patch
from biomerte import stream_router
from biomerte.control_clock import ControlClock

class StartupTests(unittest.IsolatedAsyncioTestCase):
 async def test_listener_opens_while_ntp_is_unavailable(self):
  listening=asyncio.Event();closed=asyncio.Event()
  class PendingClock:
   ready=False
   async def start(self):await asyncio.Event().wait()
   async def close(self):pass
  class Listener:
   async def __aenter__(self):listening.set();return self
   async def __aexit__(self,*args):closed.set();return False
  ws=ModuleType("websockets");ws.__path__=[]
  wa=ModuleType("websockets.asyncio");wa.__path__=[]
  server=ModuleType("websockets.asyncio.server");server.serve=lambda *a,**k:Listener()
  ws.asyncio=wa;wa.server=server
  modules={"websockets":ws,"websockets.asyncio":wa,"websockets.asyncio.server":server}
  app=SimpleNamespace(node="test",config={"stream_router_secret":"test"})
  config=dict(controllers=["test"],control_ntp=True,transport="private_http",listen="127.0.0.1",port=0)
  with patch("biomerte.app.Application",return_value=app),patch("biomerte.stream_inspection.add_remote"),patch("biomerte.control_clock.ControlClock",PendingClock),patch.dict(sys.modules,modules):
   task=asyncio.create_task(stream_router.serve(config))
   try:
    await asyncio.wait_for(listening.wait(),1)
    self.assertFalse(task.done())
    self.assertFalse(closed.is_set())
   finally:
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    self.assertTrue(closed.is_set())
 def test_unsynchronized_control_deadlines_stay_rejected(self):
  clock=ControlClock()
  self.assertFalse(clock.ready)
  with self.assertRaises(RuntimeError):clock.upper()
  with self.assertRaises(RuntimeError):clock.lower()

 async def test_clock_retries_initial_network_failure(self):
  clock=ControlClock();calls=[]
  async def refresh():
   calls.append(1)
   if len(calls)==1:raise RuntimeError("offline")
   clock.interval=(0.,0.);clock.sampled_at=clock.monotonic()
  clock.refresh=refresh
  await clock.start()
  try:self.assertTrue(clock.ready);self.assertEqual(len(calls),2)
  finally:await clock.close()
