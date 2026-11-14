import copy
import math
import tempfile
import time
import unittest
from pathlib import Path
import sys

import numpy as np
from scipy.spatial.transform import Rotation

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tof_registration import FrameRegistration, fit_ranges, floor_from_motion, matrix, transform


def fixture():
    anchors = {str(i): np.array(p, float) for i, p in enumerate([
        [-3, -2, 1], [4, -1, 2], [-2, 4, 3], [3, 3, -2], [1, -4, 4]])}
    r = Rotation.from_euler('xyz', [.35, -.2, .8]).as_matrix()
    t = np.eye(4); t[:3, :3] = 1.7*r; t[:3, 3] = [1.2, -.7, .3]
    rows = []
    for i in range(12):
        theta = i*2*math.pi/12
        for j, (key, a) in enumerate(anchors.items()):
            p = np.array([math.cos(theta)*1.5, math.sin(theta), .1])
            d = np.linalg.norm(transform(t, p)-a)+.008*math.sin(i*7+j)
            rows.append(dict(anchor=key, point=p.tolist(), time_s=i*10+j, distance_m=float(d)))
    return anchors, t, rows


class RangeRegistrationTests(unittest.TestCase):
    def test_async_ranges_fit_metric_similarity_without_instantaneous_rover_fix(self):
        anchors, expected, rows = fixture()
        actual = fit_ranges(rows, anchors)
        self.assertTrue(actual['valid'], actual)
        self.assertLess(actual['validation_rms_m'], .03)
        np.testing.assert_allclose(actual['T_anchor_map'], expected, atol=.03)
        self.assertEqual(actual['validation_count'], 10)

    def test_stationary_and_straight_motion_are_not_registration(self):
        anchors, _, rows = fixture()
        for straight in [False, True]:
            samples = copy.deepcopy(rows)
            for i, row in enumerate(samples): row['point'] = [i/30 if straight else 0, 0, 0]
            self.assertFalse(fit_ranges(samples, anchors)['valid'])

    def test_coplanar_anchors_keep_mirror_ambiguity(self):
        anchors, _, rows = fixture()
        for p in anchors.values(): p[2] = 0
        result = fit_ranges(rows, anchors)
        self.assertFalse(result['valid']); self.assertEqual(result['status'], 'anchor_mirror_ambiguity')

    def test_held_out_failure_does_not_become_valid(self):
        anchors, _, rows = fixture()
        counters = {}
        for row in rows:
            n = counters.get(row['anchor'], 0); counters[row['anchor']] = n+1
            if n % 5 == 4: row['distance_m'] += 8
        result = fit_ranges(rows, anchors)
        self.assertFalse(result['valid']); self.assertGreater(result['validation_rms_m'], result['acceptance_m'])

    def test_signed_floor_uses_motion_and_gravity_not_equal_anchor_heights(self):
        theta = np.linspace(0, 2*math.pi, 24)
        points = np.array([[math.cos(a), .0635, math.sin(a)] for a in theta])
        result = floor_from_motion(points, np.eye(3), .0635, up=[0, 1, 0])
        self.assertTrue(result['valid'], result)
        mapped = transform(matrix(result['T_floor_anchor']), points)
        np.testing.assert_allclose(mapped[:, 1], .0635, atol=1e-7)
        self.assertFalse(floor_from_motion(points, np.eye(3), .0635)['valid'])

    def test_nonplanar_motion_and_conflicting_gravity_stay_unknown(self):
        points = np.array([[math.cos(a), .0635, math.sin(a)] for a in np.linspace(0, 2*math.pi, 24)])
        self.assertFalse(floor_from_motion(points, np.eye(3), .0635, up=[1, 0, 0])['valid'])
        points[::2, 1] += 1
        self.assertFalse(floor_from_motion(points, np.eye(3), .0635, up=[0, 1, 0])['valid'])


class CollectionAndPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'registration.json'
        self.anchors = {}
        for key, xyz in zip('abcd', [[0,0,0], [2,0,0], [0,0,2], [1,1,1]]):
            t = np.eye(4); t[:3, 3] = xyz; self.anchors[key] = {'id':key, 'T_map_keyframe':t.tolist()}
        self.reg = FrameRegistration(self.path, lambda m,k,r:self.anchors.get(k))
        self.tof = {'collector_age_s':0, 'valid':False, 'baseline':{'valid':True, 'epoch':2, 'revision':3, 'frame_id':'f', 'positions':[]}, 'rover_ranges':[]}

    def tearDown(self):
        self.reg.close(); self.tmp.cleanup()

    def accepted(self):
        return {'valid':True,'status':'range_motion_registered','map_id':'m','map_revision':1,
                'frame_id':'f','baseline_epoch':2,'baseline_revision':3,'T_anchor_map':np.eye(4).tolist(),
                'controls':{k:v['T_map_keyframe'] for k,v in self.anchors.items()},'navigation_authority':False}

    def test_persistence_and_current_keyframe_reprojection(self):
        self.reg.attachments['m'] = self.accepted(); self.reg._save()
        restored = FrameRegistration(self.path, lambda m,k,r:self.anchors.get(k))
        try:
            self.assertTrue(restored.status(self.tof, 'm', 1)['valid'])
            for a in self.anchors.values():
                a['T_map_keyframe'][0][3] += 4
            result = restored.status(self.tof, 'm', 2)
            self.assertTrue(result['valid'], result)
            np.testing.assert_allclose(np.array(result['T_anchor_map'])[:3,3], [-4,0,0], atol=1e-7)
            self.anchors['d']['T_map_keyframe'][1][3] += 3
            self.assertFalse(restored.status(self.tof, 'm', 3)['valid'])
        finally: restored.close()

    def test_epoch_revision_and_stale_baseline_withhold_attachment(self):
        self.reg.attachments['m'] = self.accepted()
        for key, val in [('epoch',4), ('revision',5), ('frame_id','other')]:
            tof=copy.deepcopy(self.tof); tof['baseline'][key]=val
            self.assertFalse(self.reg.status(tof,'m',1)['valid'])
        tof=copy.deepcopy(self.tof); tof['collector_age_s']=8
        self.assertFalse(self.reg.status(tof,'m',1)['valid'])

    def test_camera_stream_uses_cached_registration_without_map_database_access(self):
        self.reg.attachments['m'] = self.accepted()
        self.reg.refresh_stream_status(self.tof,'m',1)
        self.reg.resolve_anchor=lambda *args: self.fail('Independent camera stream queried map storage')
        self.assertTrue(self.reg.stream_status(self.tof,'m',1)['valid'])
        self.assertFalse(self.reg.stream_status(self.tof,'m',2)['valid'])

    def test_time_bracketing_and_duplicate_ranges_without_valid_fix(self):
        now = time.monotonic(); observed = now-.15
        raw = {'target_bssid':'radio','observed':observed,'distance_m':2.,'rtt_ns':12,'samples':12}
        tof=copy.deepcopy(self.tof); tof['rover_ranges']=[raw]
        localization={'valid':True,'map_id':'m','map_revision':1,'T_map_camera':np.eye(4).tolist()}
        for i, tm in enumerate([now-.3, now-.1, now]):
            self.reg.observe({'receive_decode_monotonic_s':tm,'frame_id':str(i)},localization,tof,self.anchors['a'])
        self.assertEqual(len(self.reg.rows),1)
        self.assertFalse(tof['valid'])
        self.assertAlmostEqual(self.reg.rows[0]['fraction'], .75, places=4)
        prior_binding=self.reg.binding
        self.reg.status(tof,'unrelated-map',1)
        self.assertEqual(self.reg.binding,prior_binding)

    def test_floor_priors_preserve_different_mounting_heights(self):
        priors={'anchors':{'pi':{'level':0,'floor':'downstairs','mount_height_m':None},'garage':{'level':0},'laundry':{'level':0},'living':{'level':1},'kitchen':{'level':1}}}
        (self.path.parent/'tof-floor-priors.json').write_text(__import__('json').dumps(priors))
        r=FrameRegistration(self.path,lambda *x:None)
        try:
            self.assertEqual(r.floor_priors,priors['anchors'])
            self.assertIsNone(r.floor_priors['pi']['mount_height_m'])
        finally:r.close()


class WorldIntegrationTests(unittest.TestCase):
    def test_registered_submap_can_join_world_without_ORB_matches(self):
        from world_atlas import WorldAtlas
        with tempfile.TemporaryDirectory() as tmp:
            atlas=WorldAtlas(Path(tmp)/'world.sqlite3', object())
            try:
                atlas.register('root'); atlas.register('other')
                atlas.db.execute("UPDATE world SET root='root'")
                atlas.db.execute("UPDATE submaps SET revision=1")
                atlas.db.execute("UPDATE submaps SET transform=? WHERE id='root'",(__import__('json').dumps(np.eye(4).tolist()),))
                atlas.db.commit(); atlas._landmarks={'root':[],'other':[]}
                reference=np.eye(4); reference[:3,3]=[3,2,1]
                other=np.eye(4); other[:3,3]=[5,2,1]
                atlas.registration_status=lambda mid,rev:{'valid':True,'frame_id':'anchor-frame','baseline_epoch':4,
                    'baseline_revision':5,'T_anchor_map':(reference if mid=='root' else other).tolist(),
                    'training_count':40,'acceptance_m':.75}
                atlas.align_pending(); status=atlas.status()
                placed=next(x for x in status['submaps'] if x['map_id']=='other')
                self.assertEqual(placed['evidence']['method'],'motion_compensated_FTM_registration')
                np.testing.assert_allclose(np.array(placed['T_world_map'])[:3,3],[2,0,0])
                self.assertTrue(status['tof_registration']['valid'])
                np.testing.assert_allclose(status['tof_registration']['T_anchor_map'],reference)
            finally:atlas.close()

    def test_stream_preserves_complete_registration_matrices(self):
        from stream_telemetry import bounded_metadata, size
        t=np.eye(4).tolist()
        packet={'tof':{'registration':{'valid':True,'status':'range_motion_registered',
            'T_anchor_map':t,'floor':{'valid':True,'T_floor_anchor':t},
            'diagnostic_blob':'x'*6000},'baseline':{'valid':True,'epoch':2,'revision':3}},'ahrs':{}}
        result=bounded_metadata(packet,maximum=1800)
        self.assertEqual(result,packet)
        np.testing.assert_allclose(result['tof']['registration']['T_anchor_map'],t)
        np.testing.assert_allclose(result['tof']['registration']['floor']['T_floor_anchor'],t)
        self.assertNotIn('diagnostics_omitted',result['tof']['registration'])


if __name__ == '__main__': unittest.main()
