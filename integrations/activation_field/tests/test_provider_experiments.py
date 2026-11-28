import unittest
from unittest.mock import patch
from activation_field.provider import adapter


class ProviderExperiments(unittest.TestCase):
    def test_start_uses_original_operation_id_without_session(self):
        payload=dict(operation='command-1',inputs=dict(request='',context=dict(action='experiment_start',experiment_config={'kind':'stability','tokens':10000})))
        with patch('activation_field.provider.api',return_value={'run_id':'a'*32,'version':0}) as api:
            result=adapter(payload)
        self.assertTrue(result['ok'])
        api.assert_called_once_with('/experiments',dict(request_id='command-1',expected_version=0,config={'kind':'stability','tokens':10000}))

    def test_status_and_invalid_id(self):
        payload=dict(operation='command-2',inputs=dict(request='',context=dict(action='experiment_status',run_id='a'*32)))
        with patch('activation_field.provider.api',return_value={'status':'running'}) as api:
            self.assertEqual(adapter(payload)['status'],'running')
            api.assert_called_once_with('/experiments/'+'a'*32)
            payload['inputs']['context']['run_id']='../health'
            with self.assertRaises(ValueError):adapter(payload)
            self.assertEqual(api.call_count,1)

    def test_unknown_action_does_not_create_session(self):
        with patch('activation_field.provider.api') as api:
            with self.assertRaises(ValueError):adapter(dict(operation='bad',inputs=dict(request='',context=dict(action='typo'))))
            api.assert_not_called()

if __name__=='__main__':unittest.main()
