import os
import tempfile
import unittest
from biomerte.app import Application, initialize
from biomerte.store import Fault, uid


class CapabilityUsage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        initialize(tmp.name)
        self.app = Application(tmp.name)
        self.definition = {'name': 'sensor', 'inputs': {'channel': 'str', 'fresh': 'bool?'}, 'outputs': {'value': 'int'}}
        self.offer = {'contract': 'sensor:1', 'resource': 'board', 'provider': 'reader'}
        self.call('cap.define', self.definition)
        self.call('cap.offer', self.offer)

    def call(self, op, args):
        return self.app.call(op, args, uid())

    def discover(self):
        page = self.call('cap.discover', {'contract': 'sensor:1'})
        return page['contracts'][0]['usage'], page['offers'][0]['usage']

    def test_existing_contract_has_honest_template(self):
        guide, offer = self.discover()
        self.assertEqual(guide['inputs_kind'], 'template')
        self.assertEqual(offer['calls'][0]['arguments']['resource'], 'board')
        self.assertIn('channel', offer['calls'][2]['arguments']['inputs'])
        self.assertNotIn('fresh', offer['calls'][2]['arguments']['inputs'])
        self.assertIn('value', guide['read_result'])

    def test_published_examples_survive_readvertisement_and_overlay(self):
        self.call('cap.define', dict(self.definition, usage={'example_inputs': {'channel': 'air'},
                  'read_result': 'Read value in degrees.', 'parameters': {'channel': 'Probe ID.', 'fresh': 'Force refresh.'}}))
        self.call('cap.offer', dict(self.offer, usage={'before': 'Connect the probe.', 'parameters': {'channel': 'Use air.'}}))
        self.call('cap.define', self.definition)
        self.call('cap.offer', self.offer)
        guide, offer = self.discover()
        self.assertEqual(guide['inputs_kind'], 'example')
        self.assertNotIn('before', guide)
        self.assertEqual(offer['parameters'], {'channel': 'Use air.', 'fresh': 'Force refresh.'})
        self.assertEqual(offer['calls'][2]['arguments']['inputs'], {'channel':'air'})
        self.call('cap.offer', dict(self.offer, usage={}))
        self.assertNotIn('before', self.discover()[1])
        with self.assertRaises(Fault):
            self.call('cap.define', dict(self.definition, outputs={}))

    def test_invalid_docs_do_not_replace_a_working_example(self):
        self.call('cap.define', dict(self.definition, usage={'example_inputs': {'channel': 'air'}}))
        for usage in ({'parameters': {'bad': 'no'}}, {'example_inputs': {}}, {'example_inputs': {'channel': 3}},
                      {'read_result': []}, {'unknown': 2}):
            with self.assertRaises(Fault):
                self.call('cap.offer', dict(self.offer, usage=usage))
        self.assertEqual(self.discover()[1]['inputs_kind'], 'example')
