import threading
import unittest
from unittest.mock import patch
import test_vision_service as fixture

class WorldHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisionServiceTests()
        self.f.setUp()
        self.s=self.f.make_service()
        self.f.process(self.s,self.f.frame(self.s,"initial"))
        self.old=self.s.map_id
        self.world=self.s.atlas.world_id

    def tearDown(self):
        self.f.tearDown()

    def arm(self,features=250):
        self.s.backend.continuity_state=lambda:dict(state="lost",features=features,keyframes=3,points=20,frame_index=99)
        self.s._world_lost_frames={(self.old,i) for i in range(4)}

    def test_frames_continue_while_unknown_area_native_engine_initializes(self):
        self.arm()
        entered=threading.Event();release=threading.Event()
        original=self.s._prepare_backend
        def prepare(*args,**kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test handoff was not released")
            return original(*args,**kwargs)
        failure=[]
        def run():
            try:self.s._recover_world_submap()
            except Exception as exc:failure.append(exc)
        with patch.object(self.s,"_prepare_backend",side_effect=prepare):
            worker=threading.Thread(target=run);worker.start()
            try:
                self.assertTrue(entered.wait(3))
                snapshot=self.f.process(self.s,self.f.frame(self.s,"during-world-handoff"))
                self.assertFalse(snapshot["localization"]["valid"])
                self.assertEqual(snapshot["map_id"],self.old)
                self.assertIsNone(self.s._maybe_checkpoint())
            finally:
                release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failure,[])
        self.assertNotEqual(self.s.map_id,self.old)
        self.assertEqual(self.s.atlas.world_id,self.world)
        self.assertTrue(self.s.store.read_map(self.old,kinds=["sparse_point"])["entities"])
        self.assert_recovery_session()

    def assert_recovery_session(self):
        rows=self.f.session_rows(self.s)
        self.assertEqual(rows[self.s.session_id]["state"],"running")

    def test_dark_or_featureless_frames_do_not_create_submaps(self):
        self.arm(features=0)
        self.s._recover_world_submap()
        self.assertEqual(self.s.map_id,self.old)
        self.assertEqual(self.s._world_lost_frames,set())

    def test_failed_local_initialization_restores_saved_prior_map(self):
        self.arm()
        self.f.factory.fail_next_start=True
        with self.assertRaises(RuntimeError):
            self.s._recover_world_submap()
        self.assertEqual(self.s.map_id,self.old)
        self.assertEqual(self.s.atlas.world_id,self.world)
        self.assertTrue(self.s.store.read_map(self.old,kinds=["sparse_point"])["entities"])

if __name__=="__main__":unittest.main()
