import ast
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class ApprovalGateTests(unittest.TestCase):
    def test_phone_signature_and_exact_command_are_required_on_mac(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            private = home/'private.pem'
            subprocess.run(['openssl','genrsa','-out',str(private),'2048'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            public = subprocess.check_output(['openssl','rsa','-in',str(private),'-pubout'],stderr=subprocess.DEVNULL).decode()
            tree = ast.parse(Path(__file__).with_name('maintenance_agent.py').read_text())
            functions = ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('canonical','authorized')],type_ignores=[])
            scope = dict(json=json,hashlib=hashlib,base64=base64,tempfile=tempfile,Path=Path,subprocess=subprocess,HOME=home,CONF={'site':'s','phone_device':'p','phone_key':public,'host':'mac'})
            exec(compile(functions,'maintenance_gate','exec'),scope)
            canonical = scope['canonical']
            job = {'id':'command','argv':['/bin/echo','hello'],'cwd':None}
            request = {'id':'maintenance:command','proposal_revision':1,'question':'Allow this one maintenance command on mac?\nExecutable and arguments: ["/bin/echo","hello"]\nWorking directory: service default'}
            request.update(workflow_instance_id='maintenance:command',choices=[{'id':'approve','label':'Allow this command once','meaning':'approved'},{'id':'reject','label':'Reject','meaning':'rejected'}])
            payload = {'digest':hashlib.sha256(canonical(request)).hexdigest(),'choice':'approve','proposal_revision':1}
            envelope = {'site':'s','device':'p','id':'response','command':'respond','payload':base64.b64encode(canonical(payload)).decode()}
            signature = subprocess.check_output(['openssl','dgst','-sha256','-sign',str(private)],input=canonical(envelope))
            envelope['signature'] = base64.b64encode(signature).decode()
            job.update(approval=request,confirmation={'signed_envelope':envelope})
            self.assertTrue(scope['authorized'](job))
            job['argv'] = ['/bin/echo','changed']
            self.assertFalse(scope['authorized'](job))
            job['argv'] = ['/bin/echo','hello']
            envelope['signature'] = base64.b64encode(b'forged').decode()
            self.assertFalse(scope['authorized'](job))


if __name__ == '__main__':
    unittest.main()
