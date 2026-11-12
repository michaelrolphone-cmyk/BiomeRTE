"""One persistent environment with independently tracked, geometrically joined submaps.

No camera images or checkpoint copies are stored here. Checkpoints remain owned
by VisionStore. An unaligned submap never contributes invented world coordinates.
"""
import base64
import copy
import hashlib
import hmac
import secrets
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import numpy as np

LOG = logging.getLogger("robot-vision.world")


def similarity(source, target):
    """Least-squares positive Sim(3); reject collinear/degenerate evidence."""
    a, b = np.asarray(source, float), np.asarray(target, float)
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 3 or len(a) < 3:
        raise ValueError("invalid_similarity_evidence")
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("nonfinite_similarity_evidence")
    x, y = a - a.mean(0), b - b.mean(0)
    if np.linalg.matrix_rank(x, tol=1e-8) < 2 or np.linalg.matrix_rank(y, tol=1e-8) < 2:
        raise ValueError("degenerate_similarity_evidence")
    u, singular, vt = np.linalg.svd(y.T @ x / len(a))
    signs = np.ones(3)
    signs[-1] = np.sign(np.linalg.det(u @ vt))
    rotation = (u * signs) @ vt
    scale = float(singular @ signs / np.mean(np.sum(x*x, axis=1)))
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("invalid_similarity_scale")
    transform = np.eye(4)
    transform[:3, :3] = scale * rotation
    transform[:3, 3] = b.mean(0) - scale * rotation @ a.mean(0)
    return transform


def transform_points(transform, points):
    t = np.asarray(transform, float)
    p = np.asarray(points, float)
    return p @ t[:3, :3].T + t[:3, 3]


def fit_submap(source, target, min_inliers=20):
    """Mutual distinctive ORB matches plus robust 3D similarity verification."""
    import cv2
    if len(source) < min_inliers or len(target) < min_inliers:
        return None
    sd = np.asarray([p["des"] for p in source], np.uint8)
    td = np.asarray([p["des"] for p in target], np.uint8)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    forward, backward = matcher.knnMatch(sd, td, k=2), matcher.knnMatch(td, sd, k=2)
    def distinctive(rows):
        return {pair[0].queryIdx: pair[0].trainIdx for pair in rows
                if len(pair) == 2 and pair[0].distance < 0.65 * pair[1].distance
                and pair[0].distance <= 48}
    f, b = distinctive(forward), distinctive(backward)
    pairs = [(i,j) for i,j in f.items() if b.get(j) == i]
    if len(pairs) < min_inliers:
        return None
    a = np.asarray([source[i]["xyz"] for i,j in pairs], float)
    z = np.asarray([target[j]["xyz"] for i,j in pairs], float)
    extent = float(np.linalg.norm(np.percentile(z,95,axis=0)-np.percentile(z,5,axis=0)))
    if extent < 1e-8:
        return None
    tolerance = extent * 0.02
    rng = np.random.default_rng(731)
    best = None
    for _ in range(512):
        try:
            ix = rng.choice(len(pairs),3,replace=False)
            t = similarity(a[ix], z[ix])
        except ValueError:
            continue
        mask = np.linalg.norm(transform_points(t,a)-z,axis=1) <= tolerance
        if best is None or mask.sum() > best.sum():
            best = mask
    if best is None or best.sum() < min_inliers or best.mean() < 0.7:
        return None
    try:
        t = similarity(a[best],z[best])
    except ValueError:
        return None
    errors = np.linalg.norm(transform_points(t,a)-z,axis=1)
    mask = errors <= tolerance
    if mask.sum() < min_inliers or mask.mean() < 0.7:
        return None
    # Do not accept a match supported by a single coincidental image.
    source_kfs, target_kfs = set(), set()
    for accepted,(i,j) in zip(mask,pairs):
        if accepted:
            source_kfs.update(source[i].get("keyframes",[]))
            target_kfs.update(target[j].get("keyframes",[]))
    if len(source_kfs) < 3 or len(target_kfs) < 3:
        return None
    return {"T_target_source":t.tolist(), "inliers":int(mask.sum()),
            "matches":len(pairs), "inlier_fraction":float(mask.mean()),
            "rms":float(np.sqrt(np.mean(errors[mask]**2))), "tolerance":tolerance,
            "source_keyframes":len(source_kfs), "target_keyframes":len(target_kfs),
            "method":"mutual_ORB_ratio_and_3D_Sim3_RANSAC"}


def checkpoint_landmarks(checkpoint):
    artifact = checkpoint.get("backend_artifact", {})
    if artifact.get("availability") != "available" or not artifact.get("path"):
        return []
    document = json.loads((Path(artifact["path"])/"map.json").read_text())
    result = []
    live_keyframes={str(k["id"]) for k in document["map"].get("keyframes",[]) if not k.get("_is_bad")}
    for p in document["map"].get("points",[]):
        des = p.get("des",{})
        if not isinstance(des,dict):
            continue
        if p.get("_is_bad") or des.get("type") != "npRaw" or des.get("dtype") != "uint8":
            continue
        values = np.asarray(des.get("data",[]),np.uint8).reshape(-1)
        xyz = np.asarray(p.get("pt",[]),float)
        if values.size != 32 or xyz.shape != (3,) or not np.isfinite(xyz).all():
            continue
        result.append({"id":str(p["id"]), "xyz":xyz.tolist(), "des":values.tolist(),
                       "keyframes":[str(o[0]) for o in p.get("_observations",[]) if str(o[0]) in live_keyframes]})
    return result


class WorldAtlas:
    def __init__(self, path, store):
        self.store = store
        from world_import import WorldImportQueue
        self.imports = WorldImportQueue(Path(path).parent)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path),check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS world(id TEXT PRIMARY KEY, root TEXT, revision INTEGER);
          CREATE TABLE IF NOT EXISTS atlas_meta(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS submaps(id TEXT PRIMARY KEY, world_id TEXT, revision INTEGER,
            transform TEXT, evidence TEXT, checkpoint_id TEXT);
        """)
        row = self.db.execute("SELECT id FROM world").fetchone()
        if row is None:
            self.db.execute("INSERT INTO world VALUES(?,NULL,0)",(str(uuid.uuid4()),))
            self.db.commit()
        self.world_id = self.db.execute("SELECT id FROM world").fetchone()[0]
        self.db.execute("INSERT OR IGNORE INTO atlas_meta VALUES(\'cursor_key\',?)",(secrets.token_hex(32),))
        self.db.commit()
        self.cursor_key = self.db.execute("SELECT value FROM atlas_meta WHERE key=\'cursor_key\'").fetchone()[0].encode()
        self._landmarks = {}
        self._stop = threading.Event()
        self.thread = None
        self.error = None
        self.service = None

    def register(self, map_id):
        with self.lock:
            inserted=self.db.execute("INSERT OR IGNORE INTO submaps VALUES(?,?,NULL,NULL,NULL,NULL)",
                            (map_id,self.world_id)).rowcount
            if inserted:
                self.db.execute("UPDATE world SET revision=revision+1")
            self.db.commit()

    def update(self, checkpoint):
        mid, revision = checkpoint["map_id"],checkpoint["map_revision"]
        self.register(mid)
        with self.lock:
            row = self.db.execute("SELECT revision,checkpoint_id FROM submaps WHERE id=?",(mid,)).fetchone()
            if row[0] == revision and row[1] == checkpoint["checkpoint_id"] and mid in self._landmarks:
                return
        landmarks = checkpoint_landmarks(checkpoint)
        with self.lock:
            self._landmarks[mid] = landmarks
            root = self.db.execute("SELECT root FROM world").fetchone()[0]
            if root is None and len(landmarks)>=3:
                self.db.execute("UPDATE world SET root=?",(mid,))
                self.db.execute("UPDATE submaps SET transform=?,evidence=? WHERE id=?",
                    (json.dumps(np.eye(4).tolist()),json.dumps({"method":"world_origin"}),mid))
            self.db.execute("UPDATE submaps SET transform=NULL,evidence=NULL WHERE id != (SELECT root FROM world)")
            self.db.execute("UPDATE submaps SET revision=?,checkpoint_id=? WHERE id=?",
                            (revision,checkpoint["checkpoint_id"],mid))
            self.db.execute("UPDATE world SET revision=revision+1")
            self.db.commit()
        self.align_pending()

    def align_pending(self):
        # Native optimization can revise local coordinates after a link was fitted.
        # Refit non-root links from the current checkpoints before exposing them.
        with self.lock:
            root = self.db.execute("SELECT root FROM world").fetchone()[0]
            if root is None:
                return
            connected = {root:np.eye(4)}
            evidence = {}
            pending = set(self._landmarks)-{root}
            while pending:
                progress = False
                for mid in sorted(pending):
                    candidates = []
                    for reference,world_reference in connected.items():
                        fit = fit_submap(self._landmarks[mid],self._landmarks[reference])
                        if fit:
                            t = world_reference @ np.asarray(fit["T_target_source"])
                            candidates.append((fit["inliers"],t,reference,fit))
                    if not candidates:
                        continue
                    candidates.sort(key=lambda x:x[0],reverse=True)
                    _,t,reference,fit = candidates[0]
                    # Multiple world references must agree, not vote over a contradiction.
                    if any(not np.allclose(t,other[1],rtol=.05,atol=fit["tolerance"]*3)
                           for other in candidates[1:]):
                        continue
                    connected[mid] = t
                    evidence[mid] = dict(fit,reference_map_id=reference)
                    pending.remove(mid)
                    progress=True
                    break
                if not progress:
                    break
            self.db.execute("UPDATE world SET revision=revision+1")
            for mid in self._landmarks:
                t = connected.get(mid)
                self.db.execute("UPDATE submaps SET transform=?,evidence=? WHERE id=?",
                    (None if t is None else json.dumps(t.tolist()),
                     json.dumps(evidence.get(mid,{"method":"world_origin"})) if t is not None else None,mid))
            self.db.commit()

    def status(self):
        with self.lock:
            root, revision = self.db.execute("SELECT root,revision FROM world").fetchone()
            rows = list(self.db.execute("SELECT id,revision,transform,evidence,checkpoint_id FROM submaps ORDER BY rowid"))
        return {"world_id":self.world_id,"revision":revision,"root_map_id":root,
                "units":"arbitrary","scale_status":"unobserved","floor_alignment":"unmeasured",
                "submaps":[{"map_id":m,"revision":r,"alignment":"unlocalized" if t is None else "aligned",
                            "T_world_map":None if t is None else json.loads(t),
                            "evidence":None if e is None else json.loads(e),"checkpoint_id":c}
                           for m,r,t,e,c in rows],
                "error":self.error,"storage":"membership_and_transforms_only",
                "unlocalized_rule":"Local geometry is retained but not placed in world coordinates."}

    def read(self, cursor=None, limit=500):
        limit = max(1,min(500,int(limit)))
        if cursor:
            encoded, signature = cursor.rsplit(".",1)
            if not hmac.compare_digest(signature,hmac.new(self.cursor_key,encoded.encode(),hashlib.sha256).hexdigest()):
                raise ValueError("invalid_world_cursor")
            state = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
            if state["world_id"] != self.world_id:
                raise ValueError("wrong_world_cursor")
        else:
            status=self.status()
            state={"world_id":self.world_id,"revision":status["revision"],"submaps":status["submaps"],
                   "root_map_id":status["root_map_id"],"index":0,"local_cursor":None,"sonar_offset":0}
        entities=[]
        while state["index"] < len(state["submaps"]) and len(entities)<limit:
            item=state["submaps"][state["index"]]
            if item["T_world_map"] is None or item["revision"] is None:
                state["index"]+=1
                continue
            page = ({"entities": [], "next_cursor": None} if state["local_cursor"] == "sonar_only" else
                self.store.read_map(item["map_id"],revision=item["revision"],
                kinds=["keyframe","sparse_point"],cursor=state["local_cursor"],limit=limit-len(entities)))
            t=np.asarray(item["T_world_map"])
            local_entities = list(page["entities"])
            if not page.get("next_cursor") and getattr(self, "sonar_entities", None):
                sonar = self.sonar_entities(item["map_id"], item["revision"])
                offset = state.get("sonar_offset", 0)
                room = limit-len(entities)-len(local_entities)
                local_entities.extend(sonar[offset:offset+room])
                state["sonar_offset"] = offset+max(0,room)
                if offset+room < len(sonar):
                    page["next_cursor"] = "sonar_only"
            for entity in local_entities:
                entity=copy.deepcopy(entity)
                entity["entity_id"]=item["map_id"]+":"+entity["entity_id"]
                data=entity["data"]
                data["source_map_id"]=item["map_id"]
                if entity["kind"]=="sparse_point":
                    data["xyz"]=transform_points(t,[data["xyz"]])[0].tolist()
                elif "T_map_camera" in data:
                    pose=np.asarray(data["T_map_camera"])
                    world_pose=np.eye(4)
                    scale=np.cbrt(np.linalg.det(t[:3,:3]))
                    world_pose[:3,:3]=(t[:3,:3]/scale)@pose[:3,:3]
                    world_pose[:3,3]=transform_points(t,[pose[:3,3]])[0]
                    data["T_map_camera"]=world_pose.tolist()
                    data["position"]=world_pose[:3,3].tolist()
                data["coordinate_frame"]="world_visual"
                entity.pop("bounds",None)
                entities.append(entity)
            state["local_cursor"]=page.get("next_cursor")
            if not state["local_cursor"]:
                state["index"]+=1
                state["sonar_offset"] = 0
        next_cursor=None
        if state["index"]<len(state["submaps"]):
            encoded=base64.urlsafe_b64encode(json.dumps(state,separators=(",",":")).encode()).decode()
            next_cursor=encoded+"."+hmac.new(self.cursor_key,encoded.encode(),hashlib.sha256).hexdigest()
        return {"map":{"map_id":self.world_id,"revision":state["revision"],
                       "metadata":{"kind":"continuous_world_atlas","units":"arbitrary",
                                   "scale_status":"unobserved","root_map_id":state["root_map_id"]}},
                "entities":entities,"next_cursor":next_cursor,"submaps":state["submaps"],
                "unlocalized_submaps":[x for x in state["submaps"] if x["T_world_map"] is None]}

    def start(self, service):
        if self.thread is not None:
            return
        self.service=service
        def run():
            try:
                maps=self.store.list_maps()
                for info in sorted(maps,key=lambda m:m["created_at"]):
                    self.register(info["map_id"])
                    try:
                        cp=self.store.latest_backend_checkpoint(info["map_id"])
                        if cp.get("availability")=="available":
                            self.update(cp)
                    except Exception:
                        LOG.exception("World import unavailable for %s; preserving its stored map",info["map_id"])
                while not self._stop.wait(1):
                    try:
                        cp=self.store.latest_backend_checkpoint(service.map_id)
                        if cp.get("availability")=="available":
                            self.update(cp)
                        self.imports.drain_one(self.store, self, service.map_id)
                        service._recover_world_submap()
                        self.error=None
                    except Exception as exc:
                        self.error=type(exc).__name__+":"+str(exc)[:240]
                        LOG.exception("World recovery deferred; native map and prior checkpoints preserved")
            except Exception as exc:
                self.error=type(exc).__name__+":"+str(exc)[:240]
                LOG.exception("World bootstrap failed; native mapping preserved")
        self.thread=threading.Thread(target=run,name="world-atlas",daemon=True)
        self.thread.start()

    def close(self):
        self._stop.set()
        if self.thread:
            self.thread.join()
        with self.lock:
            self.db.close()
