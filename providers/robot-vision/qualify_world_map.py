"""Read-only production-map import plus isolated native recovery qualification."""
import ast
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import types
import time
import numpy as np

from slam_backend import SlamBackend
from vision_store import VisionStore
from world_atlas import WorldAtlas

def main():
    root=pathlib.Path.home()/'.local/share/biome-robot-vision'
    runtime=json.loads((root/'runtime.json').read_text())
    ro=VisionStore.__new__(VisionStore)
    ro._closed=False
    ro._lock=threading.RLock()
    ro._db=sqlite3.connect('file:'+str(root/'observations/vision.sqlite3')+'?mode=ro',uri=True)
    ro._db.row_factory=sqlite3.Row
    def latest(mid):
        for row in ro._db.execute('SELECT payload FROM checkpoints WHERE map_id=? ORDER BY created_at DESC',(mid,)):
            cp=json.loads(row[0])
            artifact=cp.get('backend_artifact',{})
            if cp.get('availability')!='unavailable' and artifact.get('availability')=='available':
                if (pathlib.Path(artifact['path'])/'map.json').is_file():
                    return cp

    evidence={"production_map_id":runtime["map_id"],"production_database_read_only":True}
    with tempfile.TemporaryDirectory(prefix="biome-world-qualification-") as directory:
        directory=pathlib.Path(directory)
        atlas=WorldAtlas(directory/'world.sqlite3',ro)
        for info in ro.list_maps():
            atlas.register(info["map_id"])
            cp=latest(info["map_id"])
            if cp:
                atlas.update(cp)
        before=atlas.status()
        identity=atlas.world_id
        total=0
        page=atlas.read(limit=100)
        while True:
            total+=len(page['entities'])
            if not page['next_cursor']:
                break
            page=atlas.read(cursor=page['next_cursor'],limit=100)
        evidence["historical_world"]={"world_id":identity,"root_map_id":before["root_map_id"],
            "submaps":len(before["submaps"]),"aligned":sum(x["alignment"]=="aligned" for x in before["submaps"]),
            "unlocalized":sum(x["alignment"]=="unlocalized" for x in before["submaps"]),
            "world_entities":total}
        atlas.close()
        atlas=WorldAtlas(directory/'world.sqlite3',ro)
        assert atlas.world_id==identity
        assert len(atlas.status()["submaps"])==len(before["submaps"])
        atlas.close()
        evidence["world_identity_restart"]="passed"
        cp=latest(runtime["map_id"])
        artifact=pathlib.Path(cp['backend_artifact']['path'])
        manifest=json.loads((artifact/'backend_manifest.json').read_text())
        config={"calibration":manifest["calibration"],"pyslam_root":str(root/'vendor/pyslam'),"fps":5}
        backend=SlamBackend(config,root)
        print("STAGE native_start",flush=True)
        backend.start(runtime["map_id"],checkpoint=artifact)
        print("STAGE native_loaded",flush=True)
        engine=backend._slam
        from pyslam.slam.map_point import MapPoint
        from pyslam.slam.slam_commons import SlamState
        original_count=engine.map.num_points()
        marker=MapPoint(np.array([7.,8.,9.]),np.array([20.,30.,40.]))
        engine.map.add_point(marker)
        assert engine.map.num_points()==original_count+1
        lost=types.SimpleNamespace(slam=engine,map=engine.map,state=SlamState.LOST)
        tree=ast.parse((root/'vendor/pyslam/pyslam/slam/tracking.py').read_text())
        nodes=[n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(
            isinstance(t,ast.Name) and t.id in ('preserve_geometry','need_reset') for t in n.targets)]
        nodes.sort(key=lambda n:n.lineno)
        source='def decision(self):\n'+''.join('    '+line+'\n' for n in nodes for line in ast.unparse(n).splitlines())+'    return need_reset\n'
        scope={'SlamState':SlamState};exec(source,scope)
        engine.preserve_map_on_tracking_loss=False
        assert scope['decision'](lost),'the real restored small session must reproduce the old reset'
        print("STAGE reproduce_old_reset",flush=True)
        engine.reset_session()
        print("STAGE old_reset_done",flush=True)
        assert engine.map.num_points()==original_count
        marker=MapPoint(np.array([7.,8.,9.]),np.array([20.,30.,40.]))
        engine.map.add_point(marker)
        engine.preserve_map_on_tracking_loss=True
        assert not scope['decision'](lost)
        assert engine.map.num_points()==original_count+1
        target=directory/'native-checkpoint'
        print("STAGE checkpoint_capture",flush=True)
        ticket=backend.capture_checkpoint(target)
        saved=backend.write_checkpoint(ticket)
        before_ids={p.id for p in engine.map.get_points() if not p.is_bad()}
        backend.close()
        restored=SlamBackend(config,root)
        print("STAGE checkpoint_restore",flush=True)
        restored.start(runtime["map_id"],checkpoint=target)
        after_ids={p.id for p in restored._slam.map.get_points() if not p.is_bad()}
        assert after_ids==before_ids
        evidence["native_recovery"]={"baseline_points":original_count,"with_new_point":len(before_ids),
            "old_tracking_loss_reset_removed_new_point":True,"new_tracking_loss_preserved_new_point":True,
            "checkpoint_restore_exact_point_ids":True,"map_id_unchanged":restored.status()["map_id"]==runtime["map_id"]}
        restored.close()
    ro._db.close()
    evidence["completed_at_unix_s"]=time.time()
    output=pathlib.Path(__file__).parent/'validation/continuous-world-map-20261111.json'
    output.write_text(json.dumps(evidence,sort_keys=True,indent=2))
    print("QUALIFICATION",json.dumps(evidence,sort_keys=True))


if __name__ == '__main__':
    main()
