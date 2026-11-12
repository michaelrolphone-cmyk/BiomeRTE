import copy
import math
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import numpy as np

from multisensor_mapping import (AttitudePoseEstimator, PoseTimeline, SonarObstacleMap,
    attitude_sample, sonar_observations, estimated_rover_mounts, SonarMetricOdometry, sonar_image_constraint)
from world_atlas import WorldAtlas


def pose(x=0, angle=0):
    t = np.eye(4)
    c, s = math.cos(angle), math.sin(angle)
    t[:3,:3] = [[c,-s,0],[s,c,0],[0,0,1]]
    t[0,3] = x
    return t


def frame(t, angle=0, boot='boot', valid=True):
    a = {'valid': valid, 'generation': 1, 'yaw_origin_uptime_ms': 0,
         'quaternion_wxyz': [math.cos(angle/2),0,0,math.sin(angle/2)]}
    identity = {'receiver_id': 'camera', 'boot_id': boot}
    return {'sensor_packet': {**identity, 'capture_monotonic_us': int(t*1e6),
            'ahrs': {**identity, 'ahrs': {'attitude': a,
                'imu': {'valid': valid, 'observed_uptime_ms': int(t*1000)}}},
            'ultrasonic': {**identity, 'ultrasonic': {'worker_ready': True, 'paused': False,
                'sensors': [{'id': 'front', 'valid': True, 'status': 'ok', 'sequence': int(t*1000),
                             'observed_uptime_ms': int(t*1000)-50, 'distance_m': 1.}]}}}}


class AttitudeTests(unittest.TestCase):
    def test_prior_tracks_rotation_and_fusion_uses_it(self):
        estimator = AttitudePoseEstimator({'R_camera_imu': np.eye(3).tolist(), 'visual_orientation_weight': .5})
        self.assertIsNone(estimator.prior(frame(1), 'map'))
        estimator.fuse({'valid': True, 'T_map_camera': pose(3).tolist()})
        prior = estimator.prior(frame(1.1, .2), 'map')
        np.testing.assert_allclose(prior, pose(3,.2), atol=1e-8)
        fused = estimator.fuse({'valid': True, 'T_map_camera': pose(4,.1).tolist()}, prior)
        np.testing.assert_allclose(fused['T_map_camera'], pose(4,.15), atol=1e-8)
        self.assertTrue(fused['sensor_fusion']['orientation_fused'])

    def test_boot_map_staleness_and_loss_do_not_invent_translation(self):
        estimator = AttitudePoseEstimator({'R_camera_imu': np.eye(3).tolist()})
        estimator.prior(frame(1), 'map')
        estimator.fuse({'valid': True, 'T_map_camera': pose().tolist()})
        self.assertIsNone(estimator.prior(frame(1.1, boot='new'), 'map'))
        self.assertFalse(estimator.fuse({'valid': False, 'T_map_camera': None})['valid'])
        self.assertIsNone(estimator.prior(frame(1.2), 'othermap'))
        stale = frame(2)
        stale['sensor_packet']['ahrs']['ahrs']['imu']['observed_uptime_ms'] = 1000
        self.assertIsNone(attitude_sample(stale))

    def test_relocalization_reanchors_instead_of_rejecting_new_heading(self):
        estimator = AttitudePoseEstimator({'R_camera_imu': np.eye(3).tolist()})
        estimator.prior(frame(1), 'map')
        estimator.fuse({'valid': True, 'T_map_camera': pose().tolist()})
        prior = estimator.prior(frame(1.1,.1), 'map')
        result = estimator.fuse({'valid': True, 'T_map_camera': pose(5,1.5).tolist()}, prior)
        np.testing.assert_allclose(result['T_map_camera'], pose(5,1.5))
        np.testing.assert_allclose(estimator.prior(frame(1.2,.2), 'map'), pose(5,1.6))

    def test_passive_mount_learning_supports_observed_planar_rotation(self):
        estimator = AttitudePoseEstimator()
        for i in range(10):
            estimator.prior(frame(1+i*.1, i*.05), 'map')
            estimator.fuse({'valid': True, 'T_map_camera': pose(0,i*.05).tolist()})
        self.assertIsNotNone(estimator.mount)
        np.testing.assert_allclose(estimator.prior(frame(2,.5),'map'), pose(0,.5), atol=1e-7)


class SonarTests(unittest.TestCase):
    def test_capture_causal_boot_and_stale_validation(self):
        f = frame(1)
        self.assertEqual(len(sonar_observations(f)),1)
        row = f['sensor_packet']['ultrasonic']['ultrasonic']['sensors'][0]
        row['observed_uptime_ms'] = 1001
        self.assertEqual(sonar_observations(f),[])

    def test_echo_completion_after_capture_is_not_used(self):
        f = frame(1)
        row = f['sensor_packet']['ultrasonic']['ultrasonic']['sensors'][0]
        row.update(observed_monotonic_us=950123, completed_monotonic_us=1000001)
        self.assertEqual(sonar_observations(f),[])
        row['completed_monotonic_us'] = 952000
        self.assertAlmostEqual(sonar_observations(f)[0]['time_s'], .950123)
        row['observed_uptime_ms'] = 950
        f['sensor_packet']['ultrasonic']['boot_id'] = 'other'
        self.assertEqual(sonar_observations(f),[])

    def test_timeline_interpolates_sensor_time_and_rejects_gaps(self):
        timeline = PoseTimeline()
        timeline.append('map','boot',1,pose(0))
        timeline.append('map','boot',1.2,pose(2,.2))
        np.testing.assert_allclose(timeline.at(1.1,'boot'),pose(1,.1))
        self.assertIsNone(timeline.at(1.1,'otherboot'))
        self.assertIsNone(timeline.at(.9,'boot'))
        timeline.append('map','boot',3,pose(3))
        self.assertIsNone(timeline.at(2,'boot'))

    def test_rays_move_rotate_accumulate_deduplicate_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rays.sqlite3'
            mounts = {'front': {'origin_camera_m': [0,.1,0], 'direction_camera': [1,0,0]}}
            store = SonarObstacleMap(path,mounts)
            one, two = sonar_observations(frame(1)), sonar_observations(frame(2))
            self.assertEqual(store.record('map','one',one,lambda *args: pose()),1)
            self.assertEqual(store.record('map','one',one,lambda *args: pose()),0)
            store.record('map','two',two,lambda *args: pose(2,math.pi/2))
            result = store.read('map',scale_m_per_map_unit=1)
            endpoints = sorted(tuple(np.round(r['endpoint_m'],5)) for r in result['rays'])
            self.assertEqual(endpoints,[(1.,.1,0.),(1.9,1.,0.)])
            store.close()

            store = SonarObstacleMap(path,mounts)
            self.assertEqual(len(store.read('map',scale_m_per_map_unit=1)['rays']),2)
            self.assertEqual(len(store.read('other',scale_m_per_map_unit=1)['rays']),0)
            store.close()

    def test_world_pagination_keeps_sonar_points_and_transform(self):
        atlas = WorldAtlas.__new__(WorldAtlas)
        atlas.world_id, atlas.cursor_key = 'world', b'key'
        t = pose(10)
        atlas.status = lambda: {'revision': 4, 'root_map_id': 'map', 'submaps': [
            {'map_id': 'unknown', 'revision': 1, 'T_world_map': None},
            {'map_id': 'map', 'revision': 2, 'T_world_map': t.tolist()}]}
        native = [{'kind': 'sparse_point', 'entity_id': 'visual', 'data': {'xyz':[0,0,0]}}]
        atlas.store = SimpleNamespace(read_map=lambda *args, **kwargs: {'entities': native, 'next_cursor': None})
        atlas.sonar_entities = lambda *args: [{'kind': 'sparse_point', 'entity_id': 'sonar:'+str(i),
            'data': {'xyz':[i,0,0], 'source':'sonar'}} for i in range(5)]
        cursor, entities = None, []
        for _ in range(6):
            result = atlas.read(cursor=cursor,limit=2)
            entities.extend(result['entities'])
            cursor = result['next_cursor']
            if cursor is None:
                break
        self.assertIsNone(cursor)
        self.assertEqual(len(entities),6)
        self.assertEqual(len({e['entity_id'] for e in entities}),6)
        self.assertEqual(entities[-1]['data']['xyz'],[14,0,0])
        self.assertEqual(len(result['unlocalized_submaps']),1)

    def test_revision_snapshot_excludes_new_rays(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SonarObstacleMap(Path(directory)/'rays',estimated_rover_mounts())
            store.record('map','one',sonar_observations(frame(1)),lambda *args:pose(),revision=2)
            store.record('map','two',sonar_observations(frame(2)),lambda *args:pose(),revision=3)
            self.assertEqual(len(store.read('map',scale_m_per_map_unit=1,revision=2)['rays']),1)
            self.assertEqual(len(store.read('map',scale_m_per_map_unit=1,revision=3)['rays']),2)
            store.close()

    def test_anchor_reprojection_and_scale_do_not_scale_sonar_range(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SonarObstacleMap(Path(directory)/'rays',{'front': {'origin_camera_m':[0,0,0], 'direction_camera':[1,0,0]}})
            store.record('map','one',sonar_observations(frame(1)),lambda *args:pose(2),
                         {'id':'key','T_map_keyframe':pose(1).tolist()})
            self.assertEqual(store.read('map')['unresolved_observations'],1)
            result = store.read('map',scale_m_per_map_unit=2,resolve_anchor=lambda _:pose(4))
            np.testing.assert_allclose(result['rays'][0]['endpoint_m'],[11,0,0])
            self.assertEqual(store.read('map',scale_m_per_map_unit=2,resolve_anchor=lambda _:None)['unresolved_observations'],1)
            store.close()

    def test_missing_pose_is_preserved_then_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SonarObstacleMap(Path(directory)/'rays',estimated_rover_mounts())
            store.record('map','one',sonar_observations(frame(1)),lambda *args:None)
            self.assertEqual(store.read('map',scale_m_per_map_unit=1)['unresolved_observations'],1)
            store.resolve_pending('map',lambda *args:pose(1))
            result = store.read('map',scale_m_per_map_unit=1)
            self.assertEqual(len(result['rays']),1)
            self.assertEqual(result['rays'][0]['mount']['qualification'],'chassis_estimate')
            store.close()


class MetricMappingTests(unittest.TestCase):
    @staticmethod
    def room_scan(t,z=0):
        rows=[]
        for name,mount in estimated_rover_mounts().items():
            origin,direction=SonarObstacleMap._mount(mount)
            origin=origin+np.array([0,0,z])
            distances=[]
            for axis,bound in [(0,2),(0,-2),(2,3),(2,-3)]:
                if abs(direction[axis])>.01:
                    distance=(bound-origin[axis])/direction[axis]
                    if distance>0:distances.append(distance)
            rows.append({'id':name+':'+str(t),'sensor_id':name,'distance_m':min(distances),
                         'time_s':t,'age_s':.05,'boot_id':'boot','receiver_id':'camera'})
        return rows

    def test_front_sonar_height_projects_below_optical_center(self):
        mount=estimated_rover_mounts()['front']
        self.assertAlmostEqual(mount['origin_camera_m'][1],.0381)
        result=sonar_image_constraint({'sensor_id':'front','distance_m':1.,'time_s':1.},mount,
            {'validated':True,'fx':1000.,'fy':1000.,'cx':640.,'cy':360.,'distortion':[0,0,0,0,0]})
        self.assertAlmostEqual(result['beam_center_px'][0],640.)
        self.assertAlmostEqual(result['beam_center_px'][1],360+1000*.0381/1.04445)
        self.assertEqual(len(result['beam_boundary_px']),16)

    def test_metric_room_mapping_moves_origins_without_visual_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            mounts=estimated_rover_mounts()
            odometry=SonarMetricOdometry(Path(directory)/'state.json',mounts)
            ledger=SonarObstacleMap(Path(directory)/'rays',mounts)
            for t in [.9,1.,1.3,1.6,1.9]:
                observed=t-.05
                rows=self.room_scan(observed,(observed-.95)*.4)
                result=odometry.update(rows,np.eye(3),('ahrs','boot'),t)
                ledger.record('map',str(t),rows,lambda *args:None,
                    metric_pose_at=odometry.timeline.at,local_frame_id=odometry.local_frame_id)
                ledger.resolve_pending('map',lambda *args:None,
                    metric_pose_at=odometry.timeline.at,local_frame_id=odometry.local_frame_id)
            self.assertTrue(result['valid'])
            self.assertAlmostEqual(result['T_local_metric_camera'][2][3],.36,places=6)
            mapped=ledger.read('map')
            self.assertEqual(mapped['availability'],'available')
            json.dumps(mapped,allow_nan=False)
            self.assertGreaterEqual(len(mapped['rays']),12)
            front=[r for r in mapped['rays'] if r['sensor_id']=='front']
            self.assertGreater(np.ptp([r['origin_m'][2] for r in front]),.2)
            np.testing.assert_allclose([r['endpoint_m'][2] for r in front],3,atol=1e-6)
            self.assertTrue(all(r['coordinate_frame']=='local_metric' for r in mapped['rays']))
            self.assertEqual(ledger.summary('map')['availability'],'available')
            reopened=SonarMetricOdometry(Path(directory)/'state.json',mounts)
            self.assertEqual(reopened.local_frame_id,odometry.local_frame_id)
            ledger.close()

    def test_metric_visual_alignment_can_estimate_scale_from_straight_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=SonarObstacleMap(Path(directory)/'rays',estimated_rover_mounts())
            for i in range(5):
                visual=pose(i*.1)
                metric=pose(i*.3+2)
                ledger.offer_alignment('map',str(i),'metric',visual,metric)
            alignment=ledger.alignment('map','metric')
            self.assertTrue(alignment['valid'])
            self.assertAlmostEqual(alignment['scale_m_per_map_unit'],3)
            np.testing.assert_allclose(np.asarray(alignment['T_local_metric_map'])[:3,3],[2,0,0],atol=1e-8)
            ledger.close()

    def test_inconsistent_ranges_do_not_publish_a_stationary_pose(self):
        with tempfile.TemporaryDirectory() as directory:
            model=SonarMetricOdometry(Path(directory)/'state',estimated_rover_mounts())
            model.update(self.room_scan(.85),np.eye(3),'epoch',.9)
            model.update(self.room_scan(.95),np.eye(3),'epoch',1.)
            changed=self.room_scan(1.25)
            for i,row in enumerate(changed):row['distance_m']-=.4+i*.3
            result=model.update(changed,np.eye(3),'epoch',1.3)
            self.assertFalse(result['valid'])
            self.assertNotIn('T_local_metric_camera',result)


class ServiceFusionTests(unittest.TestCase):
    def test_service_consumes_frame_sensors_and_exports_persistent_obstacles(self):
        try:
            import test_vision_service as fixture_module
            from test_ahrs_source import telemetry
        except ImportError:
            self.skipTest('full service fixtures require native vision environment')
        fixture = fixture_module.VisionServiceTests(methodName='runTest')
        fixture.setUp()
        try:
            config = copy.deepcopy(fixture.config)
            config['ahrs']['enabled'] = True
            config['multisensor_mapping'] = {'ahrs': {'R_camera_imu': np.eye(3).tolist()}}
            service = fixture.make_service(config)
            for i in range(2):
                t = 1+i*.1
                raw = telemetry()
                identity = {k: raw[k] for k in ('receiver_id','boot_id')}
                raw['uptime_ms'] = int(t*1000)
                raw['ahrs']['imu']['observed_uptime_ms'] = int(t*1000)
                raw['ahrs']['attitude']['quaternion_wxyz'] = [math.cos(i*.05),0,0,math.sin(i*.05)]
                raw['ahrs']['attitude']['yaw_deg'] = math.degrees(i*.1)
                packet = {**identity, 'schema':'biome.capture.v1',
                    'capture_monotonic_us': int(t*1e6), 'served_monotonic_us': int(t*1e6)+1000,
                    'capture_clock':'esp_timer', 'frame_id':identity['boot_id']+':'+str(int(t*1e6)),
                    'width':32, 'height':24, 'ahrs':raw, 'radio':None,
                    'ultrasonic':{**identity, 'schema':1, 'sequence':i+1, 'uptime_ms':int(t*1000),
                        'ultrasonic':{'schema':'hcsr04.v1','worker_ready':True,'paused':False,
                            'sensors':[{'id':name,'valid':True,'status':'ok','sequence':i+1,
                                'observed_uptime_ms':int(t*1000)-50,'age_ms':50,
                                'observed_monotonic_us':int(t*1e6)-50000,
                                'completed_monotonic_us':int(t*1e6)-48000,
                                'distance_m':1.,'echo_us':5882} for name in ('front','left','right','rear')]}}}
                captured = fixture.frame(service, name=str(i))
                captured.metadata.update(sensor_packet=packet,frame_id=packet['frame_id'],
                    measurement_monotonic_s=t)
                if i:
                    service.backend.scripted.append({'localization':{'valid':True,'status':'tracking',
                        'T_map_camera':pose(.2,.1).tolist(),'reference_keyframe_id':'kf-0'},
                        'geometry':{'available':True,'complete_snapshot':False},'map_changes':[]})
                snapshot = fixture.process(service,captured)
            self.assertTrue(snapshot['localization']['sensor_fusion']['orientation_fused'])
            self.assertEqual(snapshot['spatial_evidence']['occupancy']['ray_count'],8)
            with patch.object(service.metric,'attachment_status',return_value={
                    'availability':'available','scale_m_per_map_unit':1.}):
                obstacles = service.query('robot.map.obstacles')
                self.assertEqual(len(obstacles['rays']),4)
                self.assertEqual(len(service._sonar_entities(service.map_id,service.revision)),4)
                front = next(r for r in obstacles['rays'] if r['sensor_id']=='front')
                expected = pose(.1,.05)[:3,:3] @ np.array([0,.0381,.04445]) + [.1,0,0]
                np.testing.assert_allclose(front['origin_m'],expected,atol=1e-7)
            service.close()
            reopened = fixture.make_service(config)
            self.assertEqual(reopened.obstacle_map.summary(service.map_id)['ray_count'],8)
        finally:
            fixture.tearDown()



if __name__ == '__main__':
    unittest.main()
