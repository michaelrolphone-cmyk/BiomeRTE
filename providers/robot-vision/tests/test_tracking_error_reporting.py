import ast,pathlib,types,unittest
from unittest.mock import Mock
root=pathlib.Path(__file__).resolve().parent
source=(root/'vision_service.py' if (root/'vision_service.py').exists() else root.parent/'vision_service.py').read_text()
tree=ast.parse(source)
handler=next(n for n in ast.walk(tree) if isinstance(n,ast.ExceptHandler) and any('backend_track_error:' in ast.unparse(x) for x in n.body))
code='def handle(self, exc):\n'+''.join('    '+line+'\n' for stmt in handler.body for line in ast.unparse(stmt).splitlines())
class ErrorReporting(unittest.TestCase):
 def setUp(self):
  self.clock=Mock(return_value=100);self.log=Mock();self.backend=Mock();self.scope={'time':types.SimpleNamespace(monotonic=self.clock),'LOG':self.log,'unknown_localization':lambda s:{'status':s}};exec(code,self.scope);self.obj=types.SimpleNamespace(backend=self.backend)
 def test_reason_and_limited_repeat_logging(self):
  fn=self.scope['handle'];fn(self.obj,KeyError('descriptor'));fn(self.obj,KeyError('descriptor'));self.assertEqual(self.log.exception.call_count,1);self.assertIn('KeyError',self.backend.invalidate_tracking.call_args.args[0]);self.assertIn('descriptor',self.backend.invalidate_tracking.call_args.args[0]);self.clock.return_value=131;fn(self.obj,KeyError('descriptor'));self.assertEqual(self.log.exception.call_count,2)
 def test_changed_error_logs_and_message_is_bounded(self):
  fn=self.scope['handle'];fn(self.obj,KeyError('a'));fn(self.obj,ValueError('x'*2000));self.assertEqual(self.log.exception.call_count,2);self.assertLessEqual(len(self.backend.invalidate_tracking.call_args.args[0]),532)
if __name__=='__main__':unittest.main()

