import unittest
from bundle_paths import rebase_spec,transport_chunks
class BundleRebaseTests(unittest.TestCase):
 def test_nested_paths_and_external_targets(self):
  source={'native_bundle':'/tmp/old','helper_hashes':{'mac_native.py':'h'},'phases':{'terminal':{'health_probe':['python','/tmp/old/mac_native.py','--bundle','/tmp/old']}},'target':'/production/worker.py','protected':'/tmp/older/node.json'}
  out=rebase_spec(source,['/tmp/old'],'/tmp/new')
  self.assertEqual('/tmp/new/mac_native.py',out['phases']['terminal']['health_probe'][1]);self.assertEqual('/production/worker.py',out['target']);self.assertEqual('/tmp/older/node.json',out['protected']);self.assertEqual('/tmp/old',source['native_bundle'])
 def test_unknown_old_root_rejected(self):
  with self.assertRaises(RuntimeError):rebase_spec({'native_bundle':'/tmp/new','helper_hashes':{'x.py':'h'},'argv':['/tmp/unrecognized/x.py']},['/tmp/old'],'/tmp/new')
 def test_argument_bound(self):
  self.assertEqual('a'*10001,''.join(transport_chunks('a'*10001)));self.assertLessEqual(max(map(len,transport_chunks('a'*10001))),4096)
  with self.assertRaises(ValueError):transport_chunks('x',12000)
