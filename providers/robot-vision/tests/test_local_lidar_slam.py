import unittest
import numpy as np
from local_lidar_slam import register_scan

class GeometricRegistration(unittest.TestCase):
    def test_tangential_floor_motion_is_unobservable(self):
        xy=np.array([[x,y,0.] for x in np.arange(0,.5,.08) for y in np.arange(0,.5,.08)])
        self.assertIsNone(register_scan(xy,np.eye(4),xy,np.tile([0.,0.,1.],(len(xy),1))))

    def test_two_walls_measure_horizontal_motion_and_yaw(self):
        a=np.array([[.8,t,.1] for t in np.linspace(-.6,.6,25)])
        b=np.array([[t,.8,-.2] for t in np.linspace(-.6,.6,25)])
        pts=np.vstack([a,b]);normals=np.repeat([[1.,0.,0.],[0.,1.,0.]],25,axis=0)
        T=np.eye(4);T[:2,3]=[.035,-.022]
        pose,fit=register_scan(pts,T,pts,normals)
        self.assertEqual(3,fit['observable_dof'])
        np.testing.assert_allclose(pose,np.eye(4),atol=1e-6)
