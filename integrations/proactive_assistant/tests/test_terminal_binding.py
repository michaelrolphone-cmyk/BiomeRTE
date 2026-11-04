import json
import subprocess
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_delivery import Delivery, R
from biomerte import assistant_support as a
from biomerte.store import Fault

class TerminalBinding(Delivery):
    def command(self, arguments):
        def prepare(tx):
            cfg=tx.need('assistant_config','current')
            cfg.update(controller_home=self.tmp.name,python_executable=sys.executable,
                       controller_source_dir=str(R.parents[1]))
            tx.put('offers','terminal-test',{'contract':'host.terminal.run:1',
                   'resource':'terminal.test','state':'ready'})
            return a.cli_action(tx,cfg,'knowledge.put',arguments)
        return self.execute(prepare)['inputs']['argv']

    def test_real_bash_transport_preserves_large_json_and_shell_literals(self):
        body={'text':'$(touch /not-executed) `literal` \' " \\ \n λ🙂'*600}
        argv=self.command({'key':'transport-test','body':body})
        self.assertEqual(argv[0],'/bin/bash')
        self.assertLessEqual(len(argv),32)
        self.assertTrue(all(len(x)<=4096 for x in argv))
        result=subprocess.run(argv,capture_output=True,text=True,check=True)
        receipt=json.loads(result.stdout)
        self.assertEqual(receipt['body'],body)
        saved=self.app.call('knowledge.get',{'key':'transport-test'})
        self.assertEqual(saved['revisions'][0]['body'],body)

    def test_transport_rejects_payload_beyond_actual_provider_capacity(self):
        with self.assertRaises(Fault):
            self.command({'key':'large','body':'x'*120000})

# Reuse setup without inheriting the entire delivery suite a second time.
for name in list(dir(Delivery)):
    if name.startswith('test_') and name not in TerminalBinding.__dict__:
        setattr(TerminalBinding,name,None)
del Delivery
if __name__=='__main__':unittest.main()
