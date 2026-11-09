"""Durable observation and map storage for the Mac vision provider.

The store is stdlib-only and does not estimate geometry or operate the robot.
All public results are JSON serializable. Coordinates and estimator quality
are preserved as supplied; unknown pose, scale and uncertainty remain unknown.

Image ownership:
  publish_observation(..., image_bytes=b"...") or image_path="/completed/frame"
  copies the exact image into an immutable store-owned blob. The caller keeps
  ownership of its input. Do not pass a file that another thread is still writing.
  get_snapshot_with_frame() reads snapshot and image under one lock and returns
  exact image bytes as data_base64. Expired IDs never resolve to another frame.

Retention:
  max_observations (default 300) is a configurable GLOBAL rolling buffer, not
  300 records per session. Keyframe/durable snapshots and their observations
  survive rolling eviction, but their images survive only while recent or
  explicitly pinned. pin_frame(frame_id, reason) is idempotent per reason;
  unpin_frame removes only that reason. Use durable=True only for actual map
  supporting evidence, not every tracked video frame. Later promotion changes
  retention policy without rewriting an already published snapshot. The snapshot's
  durable_observation field records its declaration at publication. Map entity revisions and
  checkpoint manifests are durable. Image blobs outside the retention policy
  are deleted; no continuous video archive is created.

Map updates:
  changes=[{"op":"upsert", "kind":"landmark", "entity_id":"p1",
            "data":{"position":[1,2,3]}, "bounds":[1,2,3,1,2,3]}]
  or {"op":"delete", "kind":"landmark", "entity_id":"p1"}.
  Bounds use the declared map units; 4-tuples mean min_x,min_y,max_x,max_y
  and 6-tuples mean min_x,min_y,min_z,max_x,max_y,max_z. Historical map reads
  and deltas are pinned to an exact revision. A non-null world pose or
  world-position observation requires an explicitly supplied map_revision.

A checkpoint saves a SQLite backup and, if supplied, copies/hashes the actual
backend artifact. It never claims relocalization has been qualified merely
because an artifact exists. Camera height/mounting are calibration evidence,
not proof that an independently reconstructed monocular map has metric scale.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


class RevisionConflict(ValueError):
    """The caller's expected map revision is no longer current."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _copy(value):
    return json.loads(_dump(value))


def _id(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be a nonempty string")
    return value


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(label + " must be an integer >= " + str(minimum))
    return value


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _bounds(value):
    if value is None:
        return (None,) * 6
    if isinstance(value, dict):
        value = [value.get("min_x"), value.get("min_y"), value.get("min_z"),
                 value.get("max_x"), value.get("max_y"), value.get("max_z")]
    else:
        value = list(value)
        if len(value) == 4:
            value = [value[0], value[1], None, value[2], value[3], None]
    if len(value) != 6:
        raise ValueError("bounds must have four or six coordinates")
    result = []
    for index, item in enumerate(value):
        if item is None and index in (2, 5):
            result.append(None)
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
            result.append(float(item))
        else:
            raise ValueError("bounds require finite x/y and optional finite z")
    for lo, hi in ((0, 3), (1, 4), (2, 5)):
        if (result[lo] is None) != (result[hi] is None):
            raise ValueError("both bounds of an axis must be present")
        if result[lo] is not None and result[lo] > result[hi]:
            raise ValueError("minimum bound exceeds maximum")
    return tuple(result)


def _world_position_present(record):
    if not isinstance(record, dict):
        return False
    return any(value is not None and
               (key.startswith("map_from_") or key.startswith("T_map_") or
                key in ("pose", "pose_map", "camera_pose", "map_position",
                        "position_map", "position_map_m"))
               for key, value in record.items())


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class VisionStore:
    def __init__(self, data_dir, *, max_observations=300):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.max_observations = _integer(max_observations, "max_observations", 1)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.data_dir / "frame_blobs"
        self.blob_dir.mkdir(exist_ok=True)
        self.checkpoint_dir = self.data_dir / "checkpoints"
        self.checkpoint_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "vision.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._checkpoint_integrity = {}
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
CREATE TABLE IF NOT EXISTS calibrations (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS maps (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS map_revisions (
 map_id TEXT NOT NULL REFERENCES maps(id), revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, reason TEXT NOT NULL, metadata TEXT NOT NULL,
 PRIMARY KEY(map_id, revision));
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, map_id TEXT NOT NULL REFERENCES maps(id),
 created_at TEXT NOT NULL, state TEXT NOT NULL, camera TEXT NOT NULL,
 calibration_id TEXT REFERENCES calibrations(id), next_sequence INTEGER NOT NULL DEFAULT 0,
 ended_at TEXT, end_reason TEXT);
CREATE TABLE IF NOT EXISTS frames (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 sequence INTEGER NOT NULL, payload TEXT NOT NULL, blob TEXT,
 sha256 TEXT, size_bytes INTEGER, media_type TEXT,
 UNIQUE(session_id, sequence));
CREATE TABLE IF NOT EXISTS snapshots (
 serial INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 frame_id TEXT NOT NULL UNIQUE REFERENCES frames(id) ON DELETE CASCADE,
 session_id TEXT NOT NULL REFERENCES sessions(id), map_id TEXT NOT NULL REFERENCES maps(id),
 map_revision INTEGER NOT NULL, durable INTEGER NOT NULL DEFAULT 0,
 payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
 map_id TEXT NOT NULL REFERENCES maps(id), keyframe_id TEXT,
 persistent_object_id TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS observations_keyframe ON observations(map_id,keyframe_id);
CREATE INDEX IF NOT EXISTS snapshots_session ON snapshots(session_id,serial);
CREATE TABLE IF NOT EXISTS observation_retention (
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(snapshot_id,reason));
CREATE TABLE IF NOT EXISTS frame_pins (
 frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
 reason TEXT NOT NULL, PRIMARY KEY(frame_id,reason));
CREATE TABLE IF NOT EXISTS entity_versions (
 map_id TEXT NOT NULL REFERENCES maps(id), revision INTEGER NOT NULL,
 ordinal INTEGER NOT NULL, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 deleted INTEGER NOT NULL, payload TEXT,
 min_x REAL,min_y REAL,min_z REAL,max_x REAL,max_y REAL,max_z REAL,
 PRIMARY KEY(map_id,revision,ordinal),
 UNIQUE(map_id,revision,kind,entity_id),
 FOREIGN KEY(map_id,revision) REFERENCES map_revisions(map_id,revision));
CREATE INDEX IF NOT EXISTS entity_history ON entity_versions(map_id,kind,entity_id,revision);
CREATE TABLE IF NOT EXISTS checkpoints (
 id TEXT PRIMARY KEY, map_id TEXT NOT NULL REFERENCES maps(id),
 map_revision INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
PRAGMA user_version=1;
""")
        with self._lock:
            with self._transaction():
                self._remove_orphan_blobs()
                expired = self._prune_locked()
            self._delete_blobs(expired)
        self.reconcile_checkpoint_inventory()

    def _open(self):
        if self._closed:
            raise RuntimeError("VisionStore is closed")

    @contextmanager
    def _transaction(self):
        self._open()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _map_locked(self, map_id, revision=None):
        row = self._db.execute("SELECT * FROM maps WHERE id=?", (map_id,)).fetchone()
        if row is None:
            raise KeyError("unknown map_id: " + str(map_id))
        rev = row["revision"] if revision is None else _integer(revision, "revision")
        item = self._db.execute(
            "SELECT * FROM map_revisions WHERE map_id=? AND revision=?",
            (map_id, rev)).fetchone()
        if item is None:
            raise KeyError("unknown map revision: " + str(rev))
        return {"map_id": map_id, "revision": rev, "latest_revision": row["revision"],
                "created_at": row["created_at"], "revision_created_at": item["created_at"],
                "reason": item["reason"], "metadata": json.loads(item["metadata"])}

    def create_map(self, metadata=None, map_id=None):
        map_id = _id(map_id or str(uuid.uuid4()), "map_id")
        values = {"coordinate_frame": "map", "units": "arbitrary",
                  "scale_status": "unanchored", "scale_source": None}
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            values.update(_copy(metadata))
        stamp = _now()
        with self._lock:
            with self._transaction():
                self._db.execute("INSERT INTO maps VALUES(?,?,0)", (map_id, stamp))
                self._db.execute("INSERT INTO map_revisions VALUES(?,?,?,?,?)",
                                 (map_id, 0, stamp, "created", _dump(values)))
            return self._map_locked(map_id)

    def save_calibration(self, calibration, calibration_id=None):
        if not isinstance(calibration, dict):
            raise ValueError("calibration must be an object")
        payload = _copy(calibration)
        calibration_id = _id(calibration_id or payload.get("calibration_id") or
                             str(uuid.uuid4()), "calibration_id")
        with self._lock:
            self._open()
            old = self._db.execute("SELECT * FROM calibrations WHERE id=?",
                                   (calibration_id,)).fetchone()
            if old:
                if json.loads(old["payload"]) != payload:
                    raise ValueError("calibration IDs are immutable; use a new ID")
                return self.get_calibration(calibration_id)
            with self._transaction():
                self._db.execute("INSERT INTO calibrations VALUES(?,?,?)",
                                 (calibration_id, _now(), _dump(payload)))
            return self.get_calibration(calibration_id)

    def get_calibration(self, calibration_id=None):
        with self._lock:
            self._open()
            if calibration_id is None:
                row = self._db.execute(
                    "SELECT * FROM calibrations ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
            else:
                row = self._db.execute("SELECT * FROM calibrations WHERE id=?",
                                       (calibration_id,)).fetchone()
            if row is None:
                return {"availability": "unavailable", "reason": "calibration_not_found",
                        "calibration_id": calibration_id}
            return {"availability": "available", "calibration_id": row["id"],
                    "created_at": row["created_at"], "calibration": json.loads(row["payload"])}

    def start_session(self, map_id, camera=None, calibration=None, session_id=None):
        session_id = _id(session_id or str(uuid.uuid4()), "session_id")
        calibration_id = None
        if isinstance(calibration, dict):
            calibration_id = self.save_calibration(calibration)["calibration_id"]
        elif calibration is not None:
            calibration_id = _id(calibration, "calibration_id")
            if self.get_calibration(calibration_id)["availability"] != "available":
                raise KeyError("unknown calibration_id")
        camera = _copy(camera or {})
        if not isinstance(camera, dict):
            raise ValueError("camera must be an object")
        stamp = _now()
        with self._lock:
            with self._transaction():
                self._map_locked(map_id)
                self._db.execute(
                    "INSERT INTO sessions(id,map_id,created_at,state,camera,calibration_id) "
                    "VALUES(?,?,?,?,?,?)",
                    (session_id, map_id, stamp, "running", _dump(camera), calibration_id))
            return {"session_id": session_id, "map_id": map_id, "created_at": stamp,
                    "state": "running", "camera": camera, "calibration_id": calibration_id}

    def stop_session(self, session_id, reason=None):
        with self._lock:
            with self._transaction():
                cur = self._db.execute(
                    "UPDATE sessions SET state='stopped',ended_at=?,end_reason=? WHERE id=?",
                    (_now(), reason, session_id))
                if cur.rowcount != 1:
                    raise KeyError("unknown session_id")
            return {"session_id": session_id, "state": "stopped"}

    def _write_blob(self, image):
        name = uuid.uuid4().hex + ".blob"
        temporary = self.blob_dir / (name + ".tmp")
        final = self.blob_dir / name
        with temporary.open("xb") as stream:
            stream.write(image)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        return name

    def publish_observation(self, session_id, frame, detections, localization=None, *,
                            tracks=None, spatial_evidence=None, keyframe_id=None,
                            durable=False, image_bytes=None, image_path=None,
                            image_media_type="image/jpeg"):
        if not isinstance(frame, dict) or not isinstance(detections, list):
            raise ValueError("frame must be an object and detections must be a list")
        if localization is not None and not isinstance(localization, dict):
            raise ValueError("localization must be an object or None")
        if tracks is not None and not isinstance(tracks, list):
            raise ValueError("tracks must be a list or None")
        if image_bytes is not None and image_path is not None:
            raise ValueError("provide image_bytes or image_path, not both")
        if image_path is not None:
            image = Path(image_path).read_bytes()
        elif image_bytes is not None:
            if not isinstance(image_bytes, (bytes, bytearray, memoryview)):
                raise TypeError("image_bytes must contain bytes")
            image = bytes(image_bytes)
        else:
            image = None
        if image is not None and not image:
            raise ValueError("an image must not be empty")
        media_type = _id(image_media_type, "image_media_type")
        frame = _copy(frame)
        detections = _copy(detections)
        loc = _copy(localization) if localization is not None else {
            "state": "unavailable", "availability": "unavailable",
            "reason": "localization_not_supplied", "map_from_camera_transform": None}
        track_values = _copy(tracks) if tracks is not None else []
        spatial = _copy(spatial_evidence) if spatial_evidence is not None else {
            "availability": "unavailable", "reason": "no_spatial_estimator_output", "layers": []}
        if not all(isinstance(item, dict) for item in detections + track_values):
            raise ValueError("detections and tracks must contain objects")
        snapshot_id = str(uuid.uuid4())
        frame_id = _id(frame.get("frame_id") or str(uuid.uuid4()), "frame_id")
        new_blob = None
        with self._lock:
            try:
                with self._transaction():
                    session = self._db.execute("SELECT * FROM sessions WHERE id=?",
                                               (session_id,)).fetchone()
                    if session is None:
                        raise KeyError("unknown session_id")
                    if session["state"] != "running":
                        raise ValueError("session is not running")
                    if self._db.execute("SELECT 1 FROM frames WHERE id=?", (frame_id,)).fetchone():
                        raise ValueError("frame_id is immutable and already exists")
                    map_id = session["map_id"]
                    if loc.get("map_id") not in (None, map_id):
                        raise ValueError("localization map_id differs from session map")
                    world_present = _world_position_present(loc) or any(
                        _world_position_present(item) for item in detections + track_values)
                    if world_present and loc.get("map_revision") is None:
                        raise ValueError("world coordinates require explicit map_revision")
                    map_info = self._map_locked(map_id, loc.get("map_revision"))
                    units = map_info["metadata"].get("units", "arbitrary")
                    if loc.get("units") not in (None, units):
                        raise ValueError("localization units differ from the referenced map")
                    loc["map_id"] = map_id
                    loc["map_revision"] = map_info["revision"]
                    loc.setdefault("units", units)
                    loc.setdefault("scale_status", map_info["metadata"].get("scale_status", "unknown"))
                    loc.setdefault("covariance", None)
                    loc.setdefault("uncertainty_status", "not_provided")
                    for item in detections + track_values:
                        if item.get("map_revision") not in (None, map_info["revision"]):
                            raise ValueError("observation map_revision differs from localization")
                    sequence = frame.get("sequence", session["next_sequence"])
                    _integer(sequence, "frame sequence")
                    self._db.execute("UPDATE sessions SET next_sequence=? WHERE id=?",
                                     (max(sequence + 1, session["next_sequence"]), session_id))
                    stamp = _now()
                    image_meta = {"supplied": image is not None, "media_type": media_type,
                                  "sha256": _digest(image) if image is not None else None,
                                  "size_bytes": len(image) if image is not None else None}
                    frame.update({"frame_id": frame_id, "session_id": session_id,
                                  "sequence": sequence, "image": image_meta})
                    frame.setdefault("captured_at", None)
                    frame.setdefault("received_at", None)
                    frame.setdefault("capture_timestamp_source", "not_provided")
                    observations = []
                    for item in detections:
                        item.setdefault("detection_id", str(uuid.uuid4()))
                        item["observation_id"] = str(uuid.uuid4())
                        observations.append(item)
                    snapshot = {
                        "availability": "available", "snapshot_id": snapshot_id,
                        "session_id": session_id, "frame_id": frame_id,
                        "map_id": map_id, "map_revision": map_info["revision"],
                        "calibration_id": session["calibration_id"],
                        "published_at": stamp, "frame": frame, "localization": loc,
                        "detections": observations, "tracks": track_values,
                        "tracks_availability": "available" if tracks is not None else "unavailable",
                        "spatial_evidence": spatial, "keyframe_id": keyframe_id,
                        "durable_observation": bool(durable or keyframe_id is not None)}
                    encoded = _dump(snapshot)
                    if image is not None:
                        new_blob = self._write_blob(image)
                    self._db.execute("INSERT INTO frames VALUES(?,?,?,?,?,?,?,?)",
                                     (frame_id, session_id, sequence, _dump(frame), new_blob,
                                      image_meta["sha256"], image_meta["size_bytes"], media_type))
                    self._db.execute(
                        "INSERT INTO snapshots(id,frame_id,session_id,map_id,map_revision,durable,payload) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (snapshot_id, frame_id, session_id, map_id, map_info["revision"],
                         int(snapshot["durable_observation"]), encoded))
                    for item in observations:
                        self._db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?)",
                                         (item["observation_id"], snapshot_id, frame_id, map_id,
                                          keyframe_id, item.get("persistent_object_id"), _dump(item)))
                    expired = self._prune_locked()
                self._delete_blobs(expired)
                return _copy(snapshot)
            except BaseException:
                if new_blob is not None:
                    self._delete_blobs([new_blob])
                raise

    def _prune_locked(self):
        recent = {row[0] for row in self._db.execute(
            "SELECT frame_id FROM snapshots ORDER BY serial DESC LIMIT ?",
            (self.max_observations,))}
        rows = self._db.execute(
            "SELECT f.id,f.blob,s.durable FROM frames f JOIN snapshots s ON s.frame_id=f.id "
            "WHERE NOT EXISTS(SELECT 1 FROM frame_pins p WHERE p.frame_id=f.id)").fetchall()
        expired = []
        for row in rows:
            if row["id"] in recent:
                continue
            if row["blob"] is not None:
                expired.append(row["blob"])
            if row["durable"]:
                self._db.execute("UPDATE frames SET blob=NULL WHERE id=?", (row["id"],))
            else:
                self._db.execute("DELETE FROM frames WHERE id=?", (row["id"],))
        return expired

    def _delete_blobs(self, names):
        for name in names:
            try:
                (self.blob_dir / name).unlink(missing_ok=True)
            except OSError:
                # A future open or successful maintenance pass removes orphan blobs.
                pass

    def _remove_orphan_blobs(self):
        referenced = {row[0] for row in self._db.execute(
            "SELECT blob FROM frames WHERE blob IS NOT NULL")}
        for path in self.blob_dir.iterdir():
            if path.is_file() and (path.name.endswith(".tmp") or
                                  path.name.endswith(".blob") and path.name not in referenced):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _snapshot_locked(self, snapshot_id=None, session_id=None, map_id=None):
        query = "SELECT payload FROM snapshots WHERE 1=1"
        args = []
        for column, value in (("id", snapshot_id), ("session_id", session_id), ("map_id", map_id)):
            if value is not None:
                query += " AND " + column + "=?"
                args.append(value)
        row = self._db.execute(query + " ORDER BY serial DESC LIMIT 1", args).fetchone()
        if row is None:
            return {"availability": "unavailable",
                    "reason": "snapshot_not_retained_or_unknown" if snapshot_id else "no_observation",
                    "snapshot_id": snapshot_id, "session_id": session_id, "map_id": map_id}
        return json.loads(row["payload"])

    def latest_snapshot(self, session_id=None, map_id=None):
        with self._lock:
            self._open()
            return self._snapshot_locked(session_id=session_id, map_id=map_id)

    def get_snapshot(self, snapshot_id):
        with self._lock:
            self._open()
            return self._snapshot_locked(snapshot_id=_id(snapshot_id, "snapshot_id"))

    def _frame_locked(self, frame_id, include_data=False):
        row = self._db.execute("SELECT * FROM frames WHERE id=?", (frame_id,)).fetchone()
        if row is None:
            return {"availability": "unavailable", "frame_id": frame_id,
                    "reason": "frame_not_retained_or_unknown"}
        result = json.loads(row["payload"])
        result.update({"media_type": row["media_type"], "sha256": row["sha256"],
                       "size_bytes": row["size_bytes"]})
        if row["blob"] is None:
            result.update({"availability": "unavailable",
                           "reason": "image_expired_or_not_supplied"})
            return result
        try:
            if include_data:
                data = (self.blob_dir / row["blob"]).read_bytes()
                if _digest(data) != row["sha256"]:
                    result.update({"availability": "unavailable", "reason": "image_integrity_error"})
                    return result
                result.update({"encoding": "base64",
                               "data_base64": base64.b64encode(data).decode("ascii")})
            elif not (self.blob_dir / row["blob"]).is_file():
                raise FileNotFoundError(row["blob"])
        except OSError:
            result.update({"availability": "unavailable", "reason": "image_blob_missing"})
            return result
        result["availability"] = "available"
        return result

    def get_frame(self, frame_id, include_data=False):
        with self._lock:
            self._open()
            return self._frame_locked(frame_id, include_data)

    def get_snapshot_with_frame(self, snapshot_id=None, session_id=None, map_id=None):
        with self._lock:
            self._open()
            snapshot = self._snapshot_locked(snapshot_id, session_id, map_id)
            if snapshot["availability"] != "available":
                return {"availability": "unavailable", "snapshot": snapshot, "frame": None}
            frame = self._frame_locked(snapshot["frame_id"], True)
            return {"availability": "available", "snapshot": snapshot, "frame": frame}

    def pin_frame(self, frame_id, reason):
        reason = _id(reason, "pin reason")
        with self._lock:
            self._open()
            frame = self._frame_locked(frame_id)
            if frame["availability"] != "available":
                return frame
            with self._transaction():
                self._db.execute("INSERT OR IGNORE INTO frame_pins VALUES(?,?)",
                                 (frame_id, reason))
            reasons = [row[0] for row in self._db.execute(
                "SELECT reason FROM frame_pins WHERE frame_id=? ORDER BY reason", (frame_id,))]
            return {"availability": "available", "frame_id": frame_id, "pin_reasons": reasons}

    def unpin_frame(self, frame_id, reason):
        with self._lock:
            with self._transaction():
                self._db.execute("DELETE FROM frame_pins WHERE frame_id=? AND reason=?",
                                 (frame_id, reason))
                expired = self._prune_locked()
            self._delete_blobs(expired)
            return self._frame_locked(frame_id)

    def retain_observation(self, snapshot_id, reason):
        """Promote supporting evidence without rewriting the published snapshot."""
        reason = _id(reason, "retention reason")
        with self._lock:
            with self._transaction():
                row = self._db.execute("SELECT 1 FROM snapshots WHERE id=?",
                                       (snapshot_id,)).fetchone()
                if row is None:
                    return {"availability": "unavailable", "reason": "snapshot_not_retained_or_unknown"}
                self._db.execute("UPDATE snapshots SET durable=1 WHERE id=?", (snapshot_id,))
                self._db.execute("INSERT OR IGNORE INTO observation_retention VALUES(?,?,?)",
                                 (snapshot_id, reason, _now()))
            reasons = [row[0] for row in self._db.execute(
                "SELECT reason FROM observation_retention WHERE snapshot_id=? ORDER BY reason",
                (snapshot_id,))]
            return {"availability": "available", "snapshot_id": snapshot_id,
                    "durable": True, "retention_reasons": reasons}

    def apply_map_update(self, map_id, expected_revision, changes, reason="mapping", metadata=None):
        _integer(expected_revision, "expected_revision")
        if not isinstance(changes, list):
            raise ValueError("changes must be a list")
        prepared = []
        seen = set()
        for change in _copy(changes):
            operation = change.get("op")
            if operation not in ("upsert", "delete"):
                raise ValueError("map change op must be upsert or delete")
            kind = _id(change.get("kind"), "entity kind")
            entity_id = _id(change.get("entity_id", change.get("id")), "entity_id")
            if (kind, entity_id) in seen:
                raise ValueError("an entity may appear only once per map revision")
            seen.add((kind, entity_id))
            data = change.get("data", {})
            if not isinstance(data, dict):
                raise ValueError("entity data must be an object")
            extent = change.get("bounds", data.get("bounds"))
            if extent is None and isinstance(data.get("position", data.get("xyz")), (list, tuple)):
                point = data.get("position", data.get("xyz"))
                if len(point) == 3:
                    extent = list(point) + list(point)
                elif len(point) == 2:
                    extent = list(point) + list(point)
            bounds = _bounds(extent) if operation == "upsert" else (None,) * 6
            prepared.append((kind, entity_id, operation == "delete",
                             None if operation == "delete" else _dump(data), bounds))
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        with self._lock:
            with self._transaction():
                current = self._map_locked(map_id)
                if current["revision"] != expected_revision:
                    raise RevisionConflict("expected revision %s; current revision %s" %
                                           (expected_revision, current["revision"]))
                if not prepared and metadata is None:
                    return {"map_id": map_id, "revision": expected_revision, "changed": False}
                values = current["metadata"]
                if metadata is not None:
                    values.update(_copy(metadata))
                revision = expected_revision + 1
                self._db.execute("INSERT INTO map_revisions VALUES(?,?,?,?,?)",
                                 (map_id, revision, _now(), str(reason), _dump(values)))
                for ordinal, (kind, entity_id, deleted, payload, bounds) in enumerate(prepared):
                    self._db.execute("INSERT INTO entity_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (map_id, revision, ordinal, kind, entity_id,
                                      int(deleted), payload, *bounds))
                self._db.execute("UPDATE maps SET revision=? WHERE id=?", (revision, map_id))
            return {"map_id": map_id, "revision": revision, "changed": True,
                    "change_count": len(prepared)}

    @staticmethod
    def _entity(row):
        bounds = [row[name] for name in ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")]
        return {"kind": row["kind"], "entity_id": row["entity_id"],
                "revision": row["revision"], "op": "delete" if row["deleted"] else "upsert",
                "data": json.loads(row["payload"]) if row["payload"] is not None else None,
                "bounds": None if all(value is None for value in bounds) else bounds}

    @staticmethod
    def _cursor(cursor, query):
        if cursor is None:
            return 0
        if isinstance(cursor, str):
            cursor = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf8"))
        if not isinstance(cursor, dict) or cursor.get("query") != query:
            raise ValueError("cursor does not match the pinned query")
        return _integer(cursor.get("offset"), "cursor offset")

    def read_map(self, map_id, revision=None, bounds=None, kinds=None, cursor=None, limit=500):
        _integer(limit, "limit", 1)
        if isinstance(cursor, str):
            cursor = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf8"))
        if revision is None and cursor is not None:
            revision = cursor.get("query", {}).get("revision")
        region = _bounds(bounds) if bounds is not None else None
        if kinds is not None:
            kinds = sorted({_id(kind, "kind") for kind in kinds})
        with self._lock:
            self._open()
            info = self._map_locked(map_id, revision)
            query_key = {"map_id": map_id, "revision": info["revision"],
                         "bounds": list(region) if region is not None else None, "kinds": kinds}
            offset = self._cursor(cursor, query_key)
            sql = (
                "SELECT e.* FROM entity_versions e JOIN "
                "(SELECT kind,entity_id,MAX(revision) revision FROM entity_versions "
                "WHERE map_id=? AND revision<=? GROUP BY kind,entity_id) v "
                "ON e.kind=v.kind AND e.entity_id=v.entity_id AND e.revision=v.revision "
                "WHERE e.map_id=? AND e.deleted=0")
            args = [map_id, info["revision"], map_id]
            if kinds is not None:
                if not kinds:
                    return {"map": info, "entities": [], "next_cursor": None}
                sql += " AND e.kind IN (" + ",".join("?" for _ in kinds) + ")"
                args.extend(kinds)
            if region is not None:
                sql += " AND e.min_x IS NOT NULL AND e.max_x>=? AND e.min_x<=? AND e.max_y>=? AND e.min_y<=?"
                args.extend([region[0], region[3], region[1], region[4]])
                if region[2] is not None:
                    sql += " AND e.min_z IS NOT NULL AND e.max_z>=? AND e.min_z<=?"
                    args.extend([region[2], region[5]])
            sql += " ORDER BY e.kind,e.entity_id LIMIT ? OFFSET ?"
            rows = self._db.execute(sql, [*args, limit + 1, offset]).fetchall()
            more = len(rows) > limit
            return {"map": info, "entities": [self._entity(row) for row in rows[:limit]],
                    "region_excludes_unlocated_entities": region is not None,
                    "next_cursor": {"query": query_key, "offset": offset + limit} if more else None}

    def read_deltas(self, map_id, since_revision, to_revision=None, cursor=None, limit=500):
        """Page ordered revision markers and entity changes; both count toward limit."""
        _integer(since_revision, "since_revision")
        _integer(limit, "limit", 1)
        if isinstance(cursor, str):
            cursor = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf8"))
        if to_revision is None and cursor is not None:
            to_revision = cursor.get("query", {}).get("to_revision")
        with self._lock:
            self._open()
            info = self._map_locked(map_id, to_revision)
            self._map_locked(map_id, since_revision)
            target = info["revision"]
            if since_revision > target:
                raise ValueError("since_revision exceeds target")
            query_key = {"map_id": map_id, "since_revision": since_revision, "to_revision": target}
            offset = self._cursor(cursor, query_key)
            sql = (
                "SELECT revision,-1 AS ordinal,'revision' AS event_type,NULL AS kind,"
                "NULL AS entity_id,0 AS deleted,NULL AS payload,"
                "NULL AS min_x,NULL AS min_y,NULL AS min_z,"
                "NULL AS max_x,NULL AS max_y,NULL AS max_z,created_at,reason,metadata "
                "FROM map_revisions WHERE map_id=? AND revision>? AND revision<=? "
                "UNION ALL "
                "SELECT revision,ordinal,'entity',kind,entity_id,deleted,payload,"
                "min_x,min_y,min_z,max_x,max_y,max_z,NULL,NULL,NULL "
                "FROM entity_versions WHERE map_id=? AND revision>? AND revision<=? "
                "ORDER BY revision,ordinal LIMIT ? OFFSET ?")
            rows = self._db.execute(sql, (map_id, since_revision, target,
                                          map_id, since_revision, target,
                                          limit + 1, offset)).fetchall()
            selected = rows[:limit]
            return {"map_id": map_id, "base_revision": since_revision, "target_revision": target,
                    "changes": [self._entity(row) for row in selected if row["event_type"] == "entity"],
                    "revisions": [{"revision": row["revision"], "created_at": row["created_at"],
                                   "reason": row["reason"], "metadata": json.loads(row["metadata"])}
                                  for row in selected if row["event_type"] == "revision"],
                    "next_cursor": {"query": query_key, "offset": offset + limit}
                                   if len(rows) > limit else None}

    def read_observations(self, map_id, keyframe_id=None, persistent_object_id=None,
                          cursor=0, limit=500):
        _integer(cursor, "cursor")
        _integer(limit, "limit", 1)
        with self._lock:
            self._open()
            self._map_locked(map_id)
            sql = "SELECT rowid,* FROM observations WHERE map_id=? AND rowid>?"
            args = [map_id, cursor]
            for column, value in (("keyframe_id", keyframe_id),
                                  ("persistent_object_id", persistent_object_id)):
                if value is not None:
                    sql += " AND " + column + "=?"
                    args.append(value)
            rows = self._db.execute(sql + " ORDER BY rowid LIMIT ?", [*args, limit + 1]).fetchall()
            result = [{"observation_id": row["id"], "snapshot_id": row["snapshot_id"],
                       "frame_id": row["frame_id"], "keyframe_id": row["keyframe_id"],
                       "persistent_object_id": row["persistent_object_id"],
                       "observation": json.loads(row["payload"])} for row in rows[:limit]]
            return {"map_id": map_id, "observations": result,
                    "next_cursor": rows[limit - 1]["rowid"] if len(rows) > limit else None}


    def checkpoint(self, map_id, backend_artifact=None, metadata=None):
        extra = _copy(metadata or {})
        if not isinstance(extra, dict):
            raise ValueError("metadata must be an object")
        with self._lock:
            self._open()
            info = self._map_locked(map_id)
            if extra.get("map_revision", info["revision"]) != info["revision"]:
                raise RevisionConflict("backend checkpoint revision differs from current map")
            checkpoint_id = str(uuid.uuid4())
            folder = self.checkpoint_dir / checkpoint_id
            staging = self.checkpoint_dir / (checkpoint_id + ".partial")
            staging.mkdir()
            published = False
            try:
                artifact = {"availability": "unavailable", "reason": "backend_artifact_not_supplied"}
                if backend_artifact is not None:
                    source = Path(backend_artifact).expanduser()
                    if not source.exists() or source.is_symlink():
                        raise ValueError("backend artifact must be an existing regular file or directory")
                    source = source.resolve()
                    if source == folder or source in folder.parents:
                        raise ValueError("backend artifact cannot contain the checkpoint destination")
                    if source.is_dir():
                        if any(path.is_symlink() for path in source.rglob("*")):
                            raise ValueError("backend artifact must not contain symlinks")
                        destination = staging / "backend"
                        shutil.copytree(source, destination)
                        files = sorted(path for path in destination.rglob("*") if path.is_file())
                    else:
                        destination = staging / "backend" / source.name
                        destination.parent.mkdir()
                        shutil.copyfile(source, destination)
                        files = [destination]
                    if not files:
                        raise ValueError("backend artifact directory is empty")
                    manifest = []
                    for path in files:
                        manifest.append({"path": str(path.relative_to(staging)),
                                         "size_bytes": path.stat().st_size,
                                         "sha256": _file_digest(path)})
                        with path.open("rb") as stream:
                            os.fsync(stream.fileno())
                    for directory in sorted((p for p in (staging / "backend").rglob("*")
                                             if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                        _sync_directory(directory)
                    _sync_directory(staging / "backend")
                    artifact = {"availability": "available",
                                "path": str(folder / destination.relative_to(staging)),
                                "files": manifest, "qualified_for_relocalization": False,
                                "qualification_reason": "saving_does_not_verify_relocalization"}
                result = {
                    "checkpoint_id": checkpoint_id, "map_id": map_id,
                    "map_revision": info["revision"], "created_at": _now(),
                    "database_path": str(folder / "vision.sqlite3"),
                    "backend_artifact": artifact, "metadata": extra,
                    "publication": "complete_v2",
                    "image_retention": "rolling_and_explicitly_pinned",
                    "relocalization_qualification": "not_tested_by_store"}
                # The finalized record contains this backup's hash. It belongs
                # in the external manifest, never self-referentially in the backup.
                self._backup_database(staging / "vision.sqlite3")
                result["database_sha256"] = _file_digest(staging / "vision.sqlite3")
                with (staging / "manifest.json").open("w", encoding="utf8") as stream:
                    stream.write(_dump(result))
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_directory(staging)
                staging.replace(folder)
                published = True
                _sync_directory(self.checkpoint_dir)
                self._record_checkpoint(result)
                return result
            except BaseException:
                # Once published, retain the complete package even if SQLite
                # inventory insertion fails. Startup can adopt that orphan.
                if not published:
                    shutil.rmtree(staging, ignore_errors=True)
                raise

    def _backup_database(self, destination):
        backup = sqlite3.connect(str(destination))
        try:
            self._db.backup(backup)
        finally:
            backup.close()
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())

    def _record_checkpoint(self, result):
        with self._transaction():
            self._db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                             (result["checkpoint_id"], result["map_id"],
                              result["map_revision"], result["created_at"], _dump(result)))

    def _checkpoint_folder(self, checkpoint_id):
        if (not isinstance(checkpoint_id, str) or not checkpoint_id
                or Path(checkpoint_id).name != checkpoint_id
                or checkpoint_id in (".", "..") or checkpoint_id.endswith(".partial")):
            raise ValueError("invalid_checkpoint_identity")
        folder = self.checkpoint_dir / checkpoint_id
        if folder.is_symlink() or folder.resolve().parent != self.checkpoint_dir:
            raise ValueError("checkpoint_path_escapes_store")
        return folder

    @staticmethod
    def _checkpoint_unavailable(result, reason):
        return {key: result[key] for key in
                ("checkpoint_id", "map_id", "map_revision", "created_at") if key in result} | {
                    "availability": "unavailable", "reason": reason}

    def _verified_checkpoint(self, result):
        if result.get("availability") == "unavailable":
            return self._checkpoint_unavailable(result, result.get("reason", "checkpoint_unavailable"))
        try:
            folder = self._checkpoint_folder(result["checkpoint_id"])
            manifest_path = folder / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            for key in ("checkpoint_id", "map_id", "map_revision", "database_path",
                        "database_sha256", "backend_artifact"):
                if manifest.get(key) != result.get(key):
                    raise ValueError("checkpoint_manifest_mismatch")
            database = folder / "vision.sqlite3"
            if Path(result["database_path"]).resolve() != database:
                raise ValueError("checkpoint_database_path_mismatch")
            expected = [(database, None, result["database_sha256"])]
            artifact = result.get("backend_artifact", {})
            if artifact.get("availability") == "available":
                backend = folder / "backend"
                destination = Path(artifact["path"]).resolve()
                if not destination.is_relative_to(backend) or not destination.exists():
                    raise ValueError("checkpoint_backend_path_mismatch")
                names = set()
                for entry in artifact["files"]:
                    name = Path(entry["path"])
                    path = folder / name
                    if name.is_absolute() or not path.resolve().is_relative_to(backend):
                        raise ValueError("checkpoint_file_escapes_package")
                    if str(name) in names:
                        raise ValueError("checkpoint_duplicate_file")
                    names.add(str(name))
                    expected.append((path, entry["size_bytes"], entry["sha256"]))
                actual = {str(path.relative_to(folder)) for path in backend.rglob("*") if path.is_file()}
                if not names or names != actual:
                    raise ValueError("checkpoint_backend_inventory_mismatch")
            if any(path.is_symlink() for path in folder.rglob("*")):
                raise ValueError("checkpoint_contains_symlink")
            fingerprint = []
            for path in [manifest_path] + [item[0] for item in expected]:
                stat = path.stat()
                if not path.is_file():
                    raise ValueError("checkpoint_missing_file")
                fingerprint.append((str(path), stat.st_dev, stat.st_ino, stat.st_size,
                                    stat.st_mtime_ns, stat.st_ctime_ns))
            fingerprint = tuple(fingerprint)
            if self._checkpoint_integrity.get(result["checkpoint_id"]) != fingerprint:
                for path, size, digest in expected:
                    if (size is not None and path.stat().st_size != size) or _file_digest(path) != digest:
                        raise ValueError("checkpoint_integrity_error")
                with sqlite3.connect("file:" + str(database) + "?mode=ro&immutable=1", uri=True) as backup:
                    row = backup.execute("SELECT revision FROM maps WHERE id=?", (result["map_id"],)).fetchone()
                    if row is None or row[0] != result["map_revision"]:
                        raise ValueError("checkpoint_database_map_revision_mismatch")
                    calibration_id = result.get("metadata", {}).get("calibration_id")
                    if calibration_id and not backup.execute(
                            "SELECT 1 FROM calibrations WHERE id=?", (calibration_id,)).fetchone():
                        raise ValueError("checkpoint_database_calibration_missing")
                backend_manifest = folder / "backend" / "backend_manifest.json"
                if artifact.get("availability") == "available" and backend_manifest.is_file():
                    native = json.loads(backend_manifest.read_text())
                    if native.get("map_id") != result["map_id"]:
                        raise ValueError("checkpoint_backend_map_mismatch")
                    if calibration_id and native.get("calibration", {}).get("calibration_id") != calibration_id:
                        raise ValueError("checkpoint_backend_calibration_mismatch")
                self._checkpoint_integrity[result["checkpoint_id"]] = fingerprint
            return {**result, "availability": "available"}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            self._checkpoint_integrity.pop(result.get("checkpoint_id"), None)
            return self._checkpoint_unavailable(result, "checkpoint_integrity_or_availability_error")

    def reconcile_checkpoint_inventory(self):
        """Adopt complete orphan publications; never promote partial/retired state."""
        with self._lock:
            self._open()
            recovered = []
            for manifest in self.checkpoint_dir.glob("*/manifest.json"):
                if manifest.parent.name.endswith(".partial"):
                    continue
                try:
                    candidate = json.loads(manifest.read_text())
                    identifier = candidate["checkpoint_id"]
                    if self._checkpoint_folder(identifier) != manifest.parent:
                        continue
                    if self._db.execute("SELECT 1 FROM checkpoints WHERE id=?", (identifier,)).fetchone():
                        continue
                    if not self._db.execute("SELECT 1 FROM maps WHERE id=?", (candidate["map_id"],)).fetchone():
                        continue
                    if not self._db.execute("SELECT 1 FROM map_revisions WHERE map_id=? AND revision=?",
                                            (candidate["map_id"], candidate["map_revision"])).fetchone():
                        continue
                    if self._verified_checkpoint(candidate)["availability"] != "available":
                        continue
                    self._record_checkpoint(candidate)
                    recovered.append(identifier)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            return {"recovered": recovered}

    def retire_superseded_checkpoints(self, *, include_legacy=False, protected_ids=()):
        """Keep a current and rollback backend per map, plus explicit references.

        Legacy records are retained unless a caller explicitly requests a
        one-time recovery audit. Retirement commits before files are removed.
        """
        with self._lock:
            self._open()
            records = [json.loads(row[0]) for row in self._db.execute(
                "SELECT payload FROM checkpoints ORDER BY created_at DESC,id DESC")]
            protected = set(protected_ids)
            newest = {}
            backends = {}
            for record in records:
                if record.get("availability") == "unavailable":
                    continue
                map_id = record["map_id"]
                need_database = map_id not in newest
                need_backend = (len(backends.get(map_id, [])) < 2
                                and record.get("backend_artifact", {}).get("availability") == "available")
                if need_database or need_backend:
                    verified = self._verified_checkpoint(record)
                    if verified["availability"] != "available":
                        continue
                    if need_database:
                        newest[map_id] = record
                        protected.add(record["checkpoint_id"])
                    if need_backend:
                        backends.setdefault(map_id, []).append(record)
                        protected.add(record["checkpoint_id"])
            retired, removed, errors = [], [], []
            for record in records:
                identifier = record["checkpoint_id"]
                already_retired = record.get("reason") == "checkpoint_retired"
                if not already_retired:
                    origin = record.get("metadata", {}).get("checkpoint_origin")
                    if (identifier in protected or origin == "explicit"
                            or (origin not in ("automatic", "recovery") and not include_legacy)):
                        continue
                    replacements = (backends.get(record["map_id"], [])
                                    if record.get("backend_artifact", {}).get("availability") == "available"
                                    else [newest.get(record["map_id"])])
                    replacement = next((item for item in replacements if item
                                        and item["map_revision"] >= record["map_revision"]
                                        and item["checkpoint_id"] != identifier), None)
                    if replacement is None or self._verified_checkpoint(replacement)["availability"] != "available":
                        continue
                    record = {**record, "availability": "unavailable",
                              "reason": "checkpoint_retired",
                              "retirement": {"retired_at": _now(),
                                             "replacement_checkpoint_id": replacement["checkpoint_id"],
                                             "policy": "current_and_rollback_per_map_plus_explicit",
                                             "legacy_audit": origin is None}}
                    with self._transaction():
                        self._db.execute("UPDATE checkpoints SET payload=? WHERE id=?",
                                         (_dump(record), identifier))
                    retired.append(identifier)
                try:
                    folder = self._checkpoint_folder(identifier)
                    if folder.exists():
                        shutil.rmtree(folder)
                        _sync_directory(self.checkpoint_dir)
                        removed.append(identifier)
                    self._checkpoint_integrity.pop(identifier, None)
                except (OSError, ValueError) as exc:
                    errors.append({"checkpoint_id": identifier, "error": type(exc).__name__})
            return {"policy": "current_and_rollback_per_map_plus_explicit",
                    "retired": retired, "removed": removed,
                    "protected": sorted(protected), "cleanup_errors": errors}


    def get_checkpoint(self, checkpoint_id):
        with self._lock:
            self._open()
            row = self._db.execute("SELECT payload FROM checkpoints WHERE id=?",
                                   (checkpoint_id,)).fetchone()
            if row is None:
                return {"availability": "unavailable", "checkpoint_id": checkpoint_id,
                        "reason": "checkpoint_not_found"}
            return self._verified_checkpoint(json.loads(row["payload"]))


    def latest_checkpoint(self, map_id):
        with self._lock:
            self._open()
            self._map_locked(map_id)
            rows = self._db.execute(
                "SELECT payload FROM checkpoints WHERE map_id=? ORDER BY created_at DESC,id DESC", (map_id,))
            for row in rows:
                result = self._verified_checkpoint(json.loads(row["payload"]))
                if result["availability"] == "available":
                    return result
            return {"availability": "unavailable", "map_id": map_id, "reason": "checkpoint_not_found"}


    def latest_backend_checkpoint(self, map_id):
        with self._lock:
            self._open()
            self._map_locked(map_id)
            rows = self._db.execute(
                "SELECT payload FROM checkpoints WHERE map_id=? ORDER BY created_at DESC,id DESC", (map_id,))
            for row in rows:
                record = json.loads(row["payload"])
                if record.get("backend_artifact", {}).get("availability") == "available":
                    result = self._verified_checkpoint(record)
                    if result["availability"] == "available":
                        return result
            return {"availability": "unavailable", "map_id": map_id, "reason": "backend_checkpoint_not_found"}


    def _checkpoint_storage_status(self):
        folders = [path for path in self.checkpoint_dir.iterdir() if path.is_dir() and not path.is_symlink()]
        return {"policy": "current_and_rollback_per_map_plus_explicit",
                "retained_packages": sum(not path.name.endswith(".partial") for path in folders),
                "partial_packages": sum(path.name.endswith(".partial") for path in folders),
                "logical_bytes": sum(path.stat().st_size for folder in folders
                                     for path in folder.rglob("*") if path.is_file() and not path.is_symlink()),
                "legacy_retention": "preserved_until_explicit_recovery_audit"}

    def status(self):
        with self._lock:
            self._open()
            counts = {}
            for name in ("maps", "sessions", "frames", "snapshots", "observations",
                         "frame_pins", "entity_versions", "checkpoints"):
                counts[name] = self._db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            blobs = list(self.blob_dir.glob("*.blob"))
            maps = [self._map_locked(row[0]) for row in self._db.execute("SELECT id FROM maps")]
            return {
                "availability": "available", "schema_version": 1,
                "database_path": str(self.db_path), "max_observations": self.max_observations,
                "counts": counts, "maps": maps,
                "checkpoint_storage": self._checkpoint_storage_status(),
                "image_blob_count": len(blobs),
                "image_bytes": sum(path.stat().st_size for path in blobs if path.exists()),
                "runtime_liveness": "not_observed_by_store",
                "estimation": "store_only; no sensor or estimator values are fabricated"}

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
