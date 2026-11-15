import unittest
from types import SimpleNamespace

import cv2
import numpy as np

from native_lidar_depth import intrinsics, pnp_pair, project_depth, sample


class NativeDepthTest(unittest.TestCase):
    def setUp(self):
        self.cal=dict(width=320,height=240,fx=240.,fy=240.,cx=160.,cy=120.,distortion=[0.]*5)
        self.mount=dict(fov_horizontal_deg=45.,fov_vertical_deg=45.)

    def test_project_only_observed_cells_and_mount(self):
        p=[dict(xyz_sensor_m=[0.,0.,1.2],zone=15)]
        T=np.eye(4);T[1,3]=-.02
        d,z=project_depth(p,self.mount,8,self.cal,T)
        self.assertAlmostEqual(float(d[116,160]),1.2,places=5)
        self.assertEqual(z[116,160],15)
        self.assertEqual(d[0,0],0.)
        self.assertEqual(z[0,0],-1)

    def test_two_observed_frames_recover_metric_pose_and_reject_wrong_depth(self):
        rng=np.random.default_rng(37)
        K=intrinsics(self.cal)
        xyz=rng.uniform([-.4,-.3,.8],[.4,.3,1.8],(130,3))
        des=rng.integers(0,256,(len(xyz),32),dtype=np.uint8)
        def frame(points):
            normalized=points[:,:2]/points[:,2,None]
            uv=normalized*np.array([240.,240.])+[160.,120.]
            return SimpleNamespace(des=des,kps=uv,kpsu=uv,kpsn=normalized,depths=points[:,2].copy(),camera=SimpleNamespace(K=K))
        a=frame(xyz);b=frame(xyz+[.025,0,0])
        yy,xx=np.indices((240,320));zones=(xx//40+8*(yy//40)).astype(np.int16)
        points,ia,ib,T=pnp_pair(a,b,zones,zones)
        self.assertGreater(len(ia),30)
        np.testing.assert_allclose(T[:3,3],[.025,0,0],atol=1e-5)
        b.depths*=2
        self.assertIsNone(pnp_pair(a,b,zones,zones))
        b.depths/=2
        self.assertIsNone(pnp_pair(a,b,zones*0,zones*0))


if __name__=='__main__':
    unittest.main()
