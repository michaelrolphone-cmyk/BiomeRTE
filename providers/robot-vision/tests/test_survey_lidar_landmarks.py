import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np

from lidar_fusion import defaults,zone_points,scale_matches
from survey_lidar_landmarks import SurveyLidarLandmarks


class SeedTest(unittest.TestCase):
    def test_survey_transform_both_mounts_and_revision_pages(self):
        with TemporaryDirectory() as td:
            root=Path(td);v=root/'vision';q=root/'biome-tof';v.mkdir();q.mkdir()
            db=sqlite3.connect(':memory:');db.execute('create table world(id text,revision integer)')
            db.execute("insert into world values('bedroom',4)")
            atlas=SimpleNamespace(lock=threading.RLock(),db=db,world_id='bedroom')
            seed=SurveyLidarLandmarks(v,atlas)
            body=np.array([[0,-1,0,.0381],[1,0,0,.638175],[0,0,1,0],[0,0,0,1.]])
            optical=np.array([[0,0,1,.047625],[-1,0,0,0],[0,-1,0,.0635],[0,0,0,1.]])
            survey=dict(reference=dict(T_enu_body=body.tolist(),T_enu_camera=(body@optical).tolist()),world_id='bedroom',survey_revision=2)
            (v/'survey-world.json').write_text(json.dumps(survey))
            now=time.time()
            (v/'survey-pose-zero.json').write_text(json.dumps(dict(world_id='bedroom',id='operator-zero',at=now-10)))
            pose=dict(world_id='bedroom',survey_zero_id='operator-zero',position_enu_m=body[:3,3].tolist(),
                      rotation_enu_body=body[:3,:3].tolist(),attitude_observed_at=now)
            nav=dict(state='stationary',stationary_reference={'imu_quiet':True},world_body_pose=pose)
            (q/'latest.json').write_text(json.dumps(dict(updated_at=now,inertial_navigation=nav)))
            cfg=defaults()['lidar']
            for i in range(12):
                rows=[]
                for sid,mount in cfg.items():
                    raw=dict(ready=True,valid=True,resolution=16,observed_monotonic_us=10000*i,
                             distance_mm=[1000]+[0]*15,target_status=[5]+[0]*15)
                    rows.append(dict(sensor_id=sid,raw=raw,mount=mount,points=zone_points(raw,mount,10000*i)))
                tick=time.monotonic()-.20+i*.005
                seed.observe(rows,dict(frame_id='frame',receiver_id='range',boot_id='boot',capture_monotonic_us=10000*i),[tick,tick],1)
            rev=db.execute('select revision from world').fetchone()[0]
            self.assertGreater(rev,4)
            self.assertEqual(seed.page(4,'',8),([],False))
            first,more=seed.page(rev,'',1);self.assertTrue(more)
            last,more=seed.page(rev,first[0]['entity_id'],1);self.assertFalse(more)
            self.assertGreater(first[0]['data']['xyz'][1],1.6)
            self.assertLess(last[0]['data']['xyz'][1],-.3)
            self.assertEqual({e['data']['sensor_id'] for e in first+last},{'front','rear'})
            depths,used=seed.visual_depths(seed.last_scan[0],rows[0])
            self.assertEqual(used,1)
            points=rows[0]['points']
            matches=scale_matches(np.asarray([points[0]['xyz_sensor_m']]),depths,cfg['front'],side=4)
            self.assertEqual(matches[0][2],0)
            self.assertAlmostEqual(matches[0][1],1.)
            changed=dict(rows[0],points=[dict(points[0],depth_m=1.5)])
            self.assertEqual(seed.visual_depths(seed.last_scan[0],changed),([],0))
            # A new zero may reuse only cells reobserved at that same surveyed pose.
            new_zero=dict(world_id='bedroom',id='second-zero',at=now-2)
            (v/'survey-pose-zero.json').write_text(json.dumps(new_zero))
            pose['survey_zero_id']=new_zero['id']
            (q/'latest.json').write_text(json.dumps(dict(updated_at=now,inertial_navigation=nav)))
            seed.snapshot=None
            with atlas.lock,db:
                for zone in range(1,5):
                    record=dict(first[0]['data'],zone=zone)
                    db.execute('insert into survey_lidar_landmarks values(?,?,?)',
                               (f"survey-lidar:2:1:front:16:{zone}",rev,json.dumps(record)))
            self.assertIsNone(seed.depth_at_verified_start('front',1,[tick,tick]))
            raw=dict(rows[0]['raw'],observed_monotonic_us=160000,distance_mm=[1000]*5+[0]*11,target_status=[5]*5+[0]*11)
            row=dict(rows[0],raw=raw,points=zone_points(raw,cfg['front'],160000))
            packet=dict(frame_id='revisit',receiver_id='range',boot_id='boot',capture_monotonic_us=160000)
            seed.observe([row],packet,[tick,tick],1)
            verified,reference=seed.depth_at_verified_start('front',1,[tick,tick])
            self.assertEqual({v['zone'] for v in verified},set(range(5)))
            self.assertEqual(reference['zero_id'],'second-zero')
            raw=dict(raw,observed_monotonic_us=170000,distance_mm=[1800]*5+[0]*11)
            row=dict(row,raw=raw,points=zone_points(raw,cfg['front'],170000))
            seed.observe([row],dict(packet,frame_id='changed',capture_monotonic_us=170000),[tick,tick],1)
            self.assertIsNone(seed.depth_at_verified_start('front',1,[tick,tick]))
            # In nonstationary operation, no newly visible cell can enter the surveyed seed.
            nav['state']='propagating'
            seed.snapshot=None
            (q/'latest.json').write_text(json.dumps(dict(updated_at=now,inertial_navigation=nav)))
            seed.observe(rows,dict(frame_id='frame',receiver_id='range',boot_id='boot',capture_monotonic_us=110000),[tick,tick],2)
            self.assertEqual(len(seed.page(rev,'',12)[0]),6)
