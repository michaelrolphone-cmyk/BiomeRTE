import copy
import json
import os
import tempfile
import unittest
import routing
import main as radio


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rows = [{'id':'rtl:usb:1-2','index':0,'serial':'00000001','unique_serial':False},
                     {'id':'rtl:usb:1-3','index':1,'serial':'00000001','unique_serial':False}]
        with open(os.path.join(self.tmp.name,'receivers.json'),'w') as f:
            json.dump({'revision':1,'receivers':{'esp:2884854ba11c':{
                'driver':'esp_sdr','serial':'28:84:85:4B:A1:1C','aliases':['a11c']}}},f)
        self.catalog = routing.Catalog(self.tmp.name,lambda:copy.deepcopy(self.rows),lambda s:'/dev/ttyACM0')

    def tearDown(self): self.tmp.cleanup()

    def configure(self, device, bands):
        return self.catalog.configure({'device':device,'antenna_ranges_hz':bands,
                                       'expected_revision':self.catalog.config['revision']})

    def test_automatic_disjoint_and_manual(self):
        self.assertEqual('esp:2884854ba11c',self.catalog.select('auto',2442000000,1000000)['id'])
        self.assertEqual('rtl:usb:1-2',self.catalog.select('auto',433920000,200000)['id'])
        self.assertEqual('explicit',self.catalog.select('a11c',2442000000,200000)['selection_basis'])
        with self.assertRaises(ValueError): self.catalog.select('0',2442000000,200000)
        with self.assertRaises(ValueError): self.catalog.select('auto',1800000000,200000)

    def test_band_preference_boundaries_and_absent_antenna(self):
        self.configure('1',[[430000000,440000000]])
        self.assertEqual('rtl:usb:1-3',self.catalog.select('auto',433920000,200000)['id'])
        self.configure('0',[[433000000,435000000]])
        self.assertEqual('rtl:usb:1-2',self.catalog.select('auto',433920000,200000)['id'])
        self.configure('0',[])
        self.assertEqual('rtl:usb:1-3',self.catalog.select('auto',433920000,200000)['id'])
        self.assertIn('antenna_warning',self.catalog.select('0',433920000,200000))
        self.assertEqual('rtl:usb:1-3',self.catalog.select('auto',434950000,200000)['id'])

    def test_stable_identity_after_index_reorder_and_restart(self):
        self.configure('1',[[430000000,440000000]])
        self.rows.reverse()
        for index,row in enumerate(self.rows): row['index']=index
        fresh = routing.Catalog(self.tmp.name,lambda:copy.deepcopy(self.rows),lambda s:'/dev/ttyACM0')
        chosen = fresh.select('auto',433920000,200000)
        self.assertEqual('rtl:usb:1-3',chosen['id'])
        self.assertEqual(0,chosen['index'])
        with self.assertRaises(ValueError):
            fresh.configure({'device':'0','expected_revision':1,'antenna_ranges_hz':[]})

    def test_disabled_offline_and_invalid_config(self):
        self.catalog.port_lookup=lambda s:None
        with self.assertRaises(ValueError): self.catalog.select('auto',2442000000,1000000)
        with self.assertRaises(ValueError): self.configure('0',[[400000000,2000000000]])
        with self.assertRaises(ValueError): self.configure('0',[[True,400000000]])
        self.catalog.configure({'device':'0','receiver_enabled':False,'expected_revision':1})
        with self.assertRaises(ValueError): self.catalog.select('0',433920000,200000)

    def test_mixed_scan_preflight_and_gain_units(self):
        ctx=object.__new__(radio.SDR);ctx.catalog=self.catalog
        cfg=radio.settings({'frequencies_hz':[433920000,2442000000]})
        self.assertEqual(['rtl','esp_sdr'],[r['driver'] for r in ctx.plan(cfg)])
        with self.assertRaises(ValueError):
            ctx.plan(radio.settings({'frequency_hz':2442000000,'gain_db':28}))
        with self.assertRaises(ValueError):
            ctx.plan(radio.settings({'frequencies_hz':[433920000,1800000000]}))


class VersionedGuidanceTests(unittest.TestCase):
    def test_v1_guidance_excludes_v2_parameters_without_changing_shared_resource(self):
        from install import versioned_offers
        offer={'contract':'radio.sdr.receive:1','resource':'radio.sdr.test','capacity':5,
               'usage':{'parameters':{'action':'devices','device':'auto',
                                      'antenna_ranges_hz':'bands','gain_index':'PHY index'}}}
        extended,legacy=versioned_offers(offer)
        self.assertEqual('radio.sdr.receive:2',extended['contract'])
        self.assertEqual('radio.sdr.receive:1',legacy['contract'])
        self.assertEqual({'action','device'},set(legacy['usage']['parameters']))
        self.assertIn('gain_index',extended['usage']['parameters'])
        self.assertIn('antenna_ranges_hz',extended['usage']['parameters'])
        self.assertEqual(legacy['resource'],extended['resource'])
        self.assertIn('gain_index',offer['usage']['parameters'])


if __name__=='__main__': unittest.main()
