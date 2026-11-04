import io
import json
import unittest
from unittest.mock import patch
import private_chat

class ThreadInferenceTests(unittest.TestCase):
    def test_system_instruction_is_forwarded_to_selected_lmstudio_model(self):
        seen=[]
        class Opener:
            def open(self,request):
                seen.append(json.loads(request.data))
                return io.StringIO(json.dumps({'model':'test-instance','choices':[{'finish_reason':'stop','message':{'content':'synthetic answer'}}]}))
        messages=[{'role':'system','content':'Thread-specific instructions'},{'role':'user','content':'synthetic request'}]
        with patch.object(private_chat,'privacy_ready'),patch.object(private_chat,'selected_model',return_value='test-instance'),patch.object(private_chat,'LOCAL',Opener()):
            self.assertEqual(private_chat.inference(messages,'selected-key')['text'],'synthetic answer')
        self.assertEqual(seen[0]['messages'],messages);self.assertEqual(seen[0]['model'],'test-instance');self.assertFalse(seen[0]['store'])
    def test_late_system_role_is_rejected_without_inference(self):
        with patch.object(private_chat,'privacy_ready'),patch.object(private_chat,'selected_model') as selected:
            with self.assertRaises(ValueError):private_chat.inference([{'role':'user','content':'x'},{'role':'system','content':'late'}],'selected-key')
            selected.assert_not_called()
