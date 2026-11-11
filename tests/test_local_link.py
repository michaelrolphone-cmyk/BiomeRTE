import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    path=os.path.join(os.path.dirname(__file__),'..','deploy',name+'.py')
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class LocalLinkTests(unittest.TestCase):
    def test_local_config_rejects_permanent_shared_backend(self):
        module=load('local_link')
        self.assertEqual(module.local_settings({'gateway_link':{'controller_id':'mac','capacity':2}})['capacity'],2)
        for key in ('gateway_journal','etcd'):
            with self.assertRaises(ValueError):module.local_settings({'gateway_link':{'controller_id':'mac'},key:{'enabled':True}})

    def test_special_jobs_verify_both_destination_identities(self):
        module=load('local_link')
        conf={'node':'application'};settings={'controller_id':'transport'}
        module.verify_destination(conf,settings,{'controller':'application','controller_owner':'transport'})
        with self.assertRaises(ValueError):module.verify_destination(conf,settings,{'controller':'other'})
        with self.assertRaises(ValueError):module.verify_destination(conf,settings,{'controller_owner':'other'})

    def test_heartbeat_reads_no_journal_position(self):
        module=load('local_link')
        body=module.heartbeat_payload({'realms':{'site':'ready'}})
        self.assertEqual(body['position'],0);self.assertTrue(body['ready'])
        self.assertFalse(module.heartbeat_payload({'realms':{'site':'unavailable'}})['ready'])

    def test_idle_maintenance_never_creates_databases_or_network_requests(self):
        module=load('session_service')
        with tempfile.TemporaryDirectory() as home:
            calls=[]
            maintain=module.maintenance_callback(home,'https://test.invalid','test-token',None,lambda *args,**kwargs:calls.append(args))
            self.assertEqual(maintain(),{'active':False})
            self.assertEqual(calls,[])
            self.assertEqual(os.listdir(home),[])
