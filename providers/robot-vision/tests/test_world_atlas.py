import copy
import json
import pathlib
import tempfile
import types
import unittest
import sys
import numpy as np

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from world_atlas import WorldAtlas, similarity, transform_points, fit_submap
from setup_slam import patch_tracking_continuity

OLD = """        need_reset = self.slam.reset_requested or (
            self.state == SlamState.LOST and self.map.num_keyframes_session() <= 5
        )"""


def landmarks(n=36, seed=1):
    rng=np.random.default_rng(seed)
    return [{"id":str(i),"xyz":rng.normal(size=3).tolist(),
             "des":rng.integers(0,256,32,dtype=np.uint8).tolist(),"keyframes":["1","2","3"]}
            for i in range(n)]


def moved(points,t):
    out=copy.deepcopy(points)
    for p in out:
        p["xyz"]=transform_points(t,[p["xyz"]])[0].tolist()
    return out


class ContinuityTests(unittest.TestCase):
    def reset_decision(self,patched,*,enabled=True,explicit=False,keyframes=4,points=100,session=2):
        scope={"SlamState":types.SimpleNamespace(LOST="lost")}
        text=patch_tracking_continuity(OLD) if patched else OLD
        exec("def decision(self):\n"+text+"\n        return need_reset",scope)
        obj=types.SimpleNamespace(state="lost",slam=types.SimpleNamespace(
            reset_requested=explicit,preserve_map_on_tracking_loss=enabled),
            map=types.SimpleNamespace(num_keyframes_session=lambda:session,
                num_keyframes=lambda:keyframes,num_points=lambda:points))
        return scope["decision"](obj)

    def test_old_path_erases_post_checkpoint_growth_new_path_relocalizes(self):
        self.assertTrue(self.reset_decision(False))
        self.assertFalse(self.reset_decision(True))
        native={"saved":set(range(80)),"new":set(range(80,150))}
        if self.reset_decision(True):
            native["new"].clear()
        self.assertEqual(len(native["saved"]|native["new"]),150)

    def test_explicit_reset_and_uninitialized_map_keep_native_behavior(self):
        self.assertTrue(self.reset_decision(True,explicit=True))
        self.assertTrue(self.reset_decision(True,keyframes=1))
        self.assertTrue(self.reset_decision(True,points=0))
        self.assertTrue(self.reset_decision(True,enabled=False))

    def test_large_sessions_keep_existing_relocalization_behavior(self):
        self.assertFalse(self.reset_decision(True,session=20))

    def test_unknown_upstream_shape_is_rejected(self):
        with self.assertRaises(RuntimeError):
            patch_tracking_continuity("different upstream source")


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.points=landmarks()
        self.t=np.eye(4)
        self.t[:3,:3]=np.array([[0,-2,0],[2,0,0],[0,0,2]])
        self.t[:3,3]=[3,-2,5]

    def test_sim3_recovers_scale_rotation_and_translation(self):
        a=[p["xyz"] for p in self.points]
        np.testing.assert_allclose(similarity(a,transform_points(self.t,a)),self.t,atol=1e-10)

    def test_real_descriptor_matching_and_outlier_rejection(self):
        target=moved(self.points,self.t)
        target.extend(landmarks(12,seed=50))
        fitted=fit_submap(self.points,target)
        self.assertIsNotNone(fitted)
        np.testing.assert_allclose(fitted["T_target_source"],self.t,atol=1e-8)

    def test_descriptor_agreement_without_geometry_does_not_join(self):
        target=copy.deepcopy(self.points)
        xyz=landmarks(seed=100)
        for p,q in zip(target,xyz):
            p["xyz"]=q["xyz"]
        self.assertIsNone(fit_submap(self.points,target))

    def test_unknown_repetitive_and_single_view_stay_unlocalized(self):
        self.assertIsNone(fit_submap(self.points,landmarks(seed=22)))
        repeated=copy.deepcopy(self.points)
        for p in repeated:
            p["des"]=[0]*32
        self.assertIsNone(fit_submap(repeated,repeated))
        single=copy.deepcopy(self.points)
        for p in single:
            p["keyframes"]=["one"]
        self.assertIsNone(fit_submap(single,single))

    def test_degenerate_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            similarity([[0,0,0],[1,0,0],[2,0,0]],[[0,0,0],[1,0,0],[2,0,0]])


class FakeStore:
    def __init__(self):
        self.maps={}
    def read_map(self,map_id,revision=None,kinds=None,cursor=None,limit=500):
        rows=copy.deepcopy(self.maps[(map_id,revision)])
        offset=int(cursor or 0)
        return {"entities":rows[offset:offset+limit],
                "next_cursor":str(offset+limit) if offset+limit<len(rows) else None}


class AtlasTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=pathlib.Path(self.temp.name)
        self.store=FakeStore()
        self.atlas=WorldAtlas(self.root/"world.sqlite3",self.store)
    def tearDown(self):
        self.atlas.close()
        self.temp.cleanup()
    def checkpoint(self,mid,points,revision=1):
        folder=self.root/(mid+str(revision))
        folder.mkdir()
        raw=[{"id":p["id"],"pt":p["xyz"],"_is_bad":False,
              "des":{"type":"npRaw","dtype":"uint8","data":p["des"]},
              "_observations":[[k,0] for k in p["keyframes"]]} for p in points]
        (folder/"map.json").write_text(json.dumps({"map":{"points":raw,"keyframes":[{"id":str(i),"_is_bad":False} for i in (1,2,3)]}}))
        self.store.maps[(mid,revision)]=[
            {"kind":"sparse_point","entity_id":p["id"],"revision":revision,"data":{"xyz":p["xyz"]}}
            for p in points]
        return {"map_id":mid,"map_revision":revision,"checkpoint_id":mid+str(revision),
                "backend_artifact":{"availability":"available","path":str(folder)}}

    def test_unlocalized_local_map_does_not_replace_world(self):
        self.atlas.update(self.checkpoint("root",landmarks()))
        self.atlas.update(self.checkpoint("unknown",landmarks(seed=9)))
        page=self.atlas.read()
        self.assertEqual(len(page["entities"]),36)
        self.assertEqual(page["unlocalized_submaps"][0]["map_id"],"unknown")
        self.assertEqual(page["map"]["map_id"],self.atlas.world_id)

    def test_matching_local_map_joins_without_discarding_world_geometry(self):
        p=landmarks()
        t=np.eye(4);t[:3,:3]*=2;t[:3,3]=[3,4,5]
        self.atlas.update(self.checkpoint("root",p))
        self.atlas.update(self.checkpoint("local",moved(p,t)))
        page=self.atlas.read()
        self.assertEqual(len(page["entities"]),72)
        self.assertEqual(page["unlocalized_submaps"],[])
        world={e["entity_id"]:e["data"]["xyz"] for e in page["entities"]}
        np.testing.assert_allclose(world["root:0"],world["local:0"],atol=1e-7)

    def test_identity_membership_and_alignment_survive_restart(self):
        self.atlas.update(self.checkpoint("root",landmarks()))
        self.atlas.register("unknown")
        identity=self.atlas.world_id
        self.atlas.close()
        self.atlas=WorldAtlas(self.root/"world.sqlite3",self.store)
        self.assertEqual(self.atlas.world_id,identity)
        self.assertEqual(len(self.atlas.status()["submaps"]),2)
        self.assertEqual(len(self.atlas.read()["entities"]),36)

    def test_world_pagination_pins_native_map_revisions(self):
        p=landmarks()
        self.atlas.update(self.checkpoint("root",p))
        first=self.atlas.read(limit=8)
        self.atlas.update(self.checkpoint("root",p[:30],revision=2))
        rows=first["entities"];cursor=first["next_cursor"]
        while cursor:
            page=self.atlas.read(cursor=cursor,limit=8)
            self.assertEqual(page["map"]["revision"],first["map"]["revision"])
            rows.extend(page["entities"]);cursor=page["next_cursor"]
        self.assertEqual(len(rows),36)

    def test_cursor_cannot_supply_an_invented_transform(self):
        self.atlas.update(self.checkpoint("root",landmarks()))
        cursor=self.atlas.read(limit=2)["next_cursor"]
        with self.assertRaises(ValueError):
            self.atlas.read(cursor=cursor[:-1]+("a" if cursor[-1]!="a" else "b"))

    def test_atlas_does_not_copy_checkpoint_packages_or_images(self):
        self.atlas.update(self.checkpoint("root",landmarks()))
        files={p.name for p in self.root.iterdir()}
        self.assertFalse(any("checkpoint" in n for n in files))
        self.assertLess((self.root/"world.sqlite3").stat().st_size,1000000)

if __name__=="__main__":
    unittest.main()
