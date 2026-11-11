import ast,asyncio,contextlib,json,pathlib,time,types,unittest
from unittest.mock import patch
source=(pathlib.Path(__file__).resolve().parents[1]/'biomerte/rover.py').read_text();tree=ast.parse(source);cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Rover');scope={'asyncio':asyncio,'contextlib':contextlib,'json':json,'time':time,'decode':lambda m,limit:(json.loads(m),None)};exec(compile(ast.Module(body=[cls],type_ignores=[]),'<actual Rover class>','exec'),scope);Rover=scope['Rover']
D={'authority':'controller','generation':3,'epoch':'epoch','attachment_credential':'test-only','url':'wss://test.invalid','limits':{'max_message_bytes':2048}}
def status(provider=True):return json.dumps({'type':'stream_status','authority':'controller','generation':3,'epoch':'epoch','provider_connected':provider,'server_connected':True,'authority_available':True})
class Wire:
 def __init__(self,rover,second):self.rover=rover;self.second=second;self.i=0;self.sent=[];self.sent_event=asyncio.Event();self.processed=asyncio.Event();self.block=asyncio.Event()
 async def __aenter__(self):return self
 async def __aexit__(self,*args):return False
 def __aiter__(self):return self
 async def __anext__(self):
  self.i+=1
  if self.i==1:return status()
  if self.i==2:self.rover.desired(1,1000);return self.second
  self.processed.set();await self.block.wait();raise StopAsyncIteration
 async def send(self,m):self.sent.append(json.loads(m));self.sent_event.set()
class Presence(unittest.IsolatedAsyncioTestCase):
 async def run_lane(self,second,expect_send):
  r=Rover(None,{},require_provider_ready=True);w=Wire(r,second)
  with patch('websockets.asyncio.client.connect',return_value=w):
   task=asyncio.create_task(r.lane(D,'control'))
   try:
    await asyncio.wait_for(w.processed.wait(),1)
    if expect_send:await asyncio.wait_for(w.sent_event.wait(),1)
    else:
     await asyncio.sleep(0);await asyncio.sleep(0);self.assertEqual(w.sent,[])
    return r,w
   finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
 async def test_unchanged_healthy_status_preserves_fresh_pending_input(self):
  r,w=await self.run_lane(status(),True);self.assertEqual(w.sent[0]['outputs']['mask'],1);self.assertEqual(w.sent[0]['epoch'],'epoch')
 async def test_unavailable_status_discards_pending_input(self):
  r,w=await self.run_lane(status(False),False);self.assertFalse(r.control_ready);self.assertTrue(r.intent.empty())
if __name__=='__main__':unittest.main()
