"""Metric-calibration persistence in the vision store's checkpointed database.

This extension deliberately shares VisionStore's lock, transaction and SQLite
connection so an ordinary map checkpoint includes calibration sessions, exact
frame evidence and applied references. It never changes native map coordinates.
"""
from __future__ import annotations

import json
import uuid
from vision_store import _copy, _dump, _now


class MetricStore:
    def __init__(self, store):
        self.store = store
        with store._lock:
            store._open()
            store._db.executescript("""
CREATE TABLE IF NOT EXISTS metric_sessions (
 id TEXT PRIMARY KEY, map_id TEXT NOT NULL REFERENCES maps(id),
 calibration_id TEXT NOT NULL REFERENCES calibrations(id),
 created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metric_jobs (
 session_id TEXT NOT NULL REFERENCES metric_sessions(id),
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
 state TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(session_id,snapshot_id));
CREATE TABLE IF NOT EXISTS metric_samples (
 session_id TEXT NOT NULL REFERENCES metric_sessions(id),
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
 payload TEXT NOT NULL, PRIMARY KEY(session_id,snapshot_id));
CREATE TABLE IF NOT EXISTS metric_attachments (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE REFERENCES metric_sessions(id),
 map_id TEXT NOT NULL REFERENCES maps(id), created_at TEXT NOT NULL,
 payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS metric_sessions_map ON metric_sessions(map_id,created_at);
CREATE INDEX IF NOT EXISTS metric_attachments_map ON metric_attachments(map_id,created_at);
""")

    def save_session(self, session):
        value = _copy(session)
        with self.store._lock:
            with self.store._transaction():
                old = self.store._db.execute("SELECT * FROM metric_sessions WHERE id=?",
                                             (value["session_id"],)).fetchone()
                if old and (old["map_id"] != value["map_id"] or
                            old["calibration_id"] != value["calibration_id"]):
                    raise ValueError("metric_session_identity_is_immutable")
                self.store._db.execute(
                    "INSERT INTO metric_sessions VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (value["session_id"], value["map_id"], value["calibration_id"],
                     value["created_at"], _dump(value)))
        return value

    def get_session(self, session_id=None, map_id=None):
        with self.store._lock:
            self.store._open()
            if session_id is not None:
                row = self.store._db.execute(
                    "SELECT payload FROM metric_sessions WHERE id=?", (session_id,)).fetchone()
            else:
                row = self.store._db.execute(
                    "SELECT payload FROM metric_sessions WHERE map_id=? "
                    "ORDER BY created_at DESC,id DESC LIMIT 1", (map_id,)).fetchone()
            if row is None:
                return None
            value = json.loads(row["payload"])
            if map_id is not None and value["map_id"] != map_id:
                raise ValueError("metric_session_map_mismatch")
            return value

    def sessions(self):
        with self.store._lock:
            self.store._open()
            return [json.loads(row[0]) for row in self.store._db.execute(
                "SELECT payload FROM metric_sessions ORDER BY created_at,id")]

    def enqueue(self, session, snapshot, *, role=None, label=None):
        sid, snap = session["session_id"], snapshot["snapshot_id"]
        job = {"session_id": sid, "snapshot_id": snap, "frame_id": snapshot["frame_id"],
               "role": role, "label": label, "state": "queued", "created_at": _now()}
        with self.store._lock:
            with self.store._transaction():
                old = self.store._db.execute(
                    "SELECT payload FROM metric_jobs WHERE session_id=? AND snapshot_id=?",
                    (sid, snap)).fetchone()
                if old:
                    old = json.loads(old[0])
                    if old.get("label") != label or (role is not None and old.get("role") != role):
                        raise ValueError("metric_capture_identity_is_immutable")
                    return old
                if label:
                    existing = [json.loads(row[0]) for row in self.store._db.execute(
                        "SELECT payload FROM metric_jobs WHERE session_id=?", (sid,))]
                    if any(row.get("label") == label for row in existing):
                        raise ValueError("metric_endpoint_label_already_used")
                # Pin the exact evidence atomically with the durable job.
                reason = "metric:" + sid
                self.store._db.execute(
                    "INSERT OR IGNORE INTO observation_retention VALUES(?,?,?)",
                    (snap, reason, _now()))
                self.store._db.execute(
                    "INSERT OR IGNORE INTO frame_pins VALUES(?,?)",
                    (snapshot["frame_id"], reason))
                self.store._db.execute("INSERT INTO metric_jobs VALUES(?,?,?,?)",
                                       (sid, snap, "queued", _dump(job)))
        return job

    def jobs(self, session_id=None, state=None):
        with self.store._lock:
            self.store._open()
            sql, args = "SELECT rowid,payload FROM metric_jobs WHERE 1=1", []
            if session_id is not None:
                sql += " AND session_id=?"
                args.append(session_id)
            if state is not None:
                sql += " AND state=?"
                args.append(state)
            return [json.loads(row["payload"]) for row in self.store._db.execute(
                sql + " ORDER BY rowid", args)]

    def finish_job(self, job, sample=None):
        with self.store._lock:
            with self.store._transaction():
                if sample is not None:
                    self.store._db.execute(
                        "INSERT INTO metric_samples VALUES(?,?,?) "
                        "ON CONFLICT(session_id,snapshot_id) DO UPDATE SET payload=excluded.payload",
                        (job["session_id"], job["snapshot_id"], _dump(sample)))
                self.store._db.execute(
                    "UPDATE metric_jobs SET state=?,payload=? WHERE session_id=? AND snapshot_id=?",
                    (job["state"], _dump(job), job["session_id"], job["snapshot_id"]))
                if sample is None and job["state"] in ("rejected", "skipped"):
                    reason = "metric:" + job["session_id"]
                    self.store._db.execute(
                        "DELETE FROM frame_pins WHERE frame_id=? AND reason=?",
                        (job["frame_id"], reason))
                    self.store._db.execute(
                        "DELETE FROM observation_retention WHERE snapshot_id=? AND reason=?",
                        (job["snapshot_id"], reason))
                expired = self.store._prune_locked()
            self.store._delete_blobs(expired)

    def samples(self, session_id):
        with self.store._lock:
            self.store._open()
            return [json.loads(row[0]) for row in self.store._db.execute(
                "SELECT payload FROM metric_samples WHERE session_id=? ORDER BY rowid",
                (session_id,))]

    def update_reference(self, session, samples):
        with self.store._lock:
            with self.store._transaction():
                for sample in samples:
                    self.store._db.execute(
                        "UPDATE metric_samples SET payload=? WHERE session_id=? AND snapshot_id=?",
                        (_dump(sample), session["session_id"], sample["snapshot_id"]))
                self.store._db.execute("UPDATE metric_sessions SET payload=? WHERE id=?",
                                       (_dump(session), session["session_id"]))

    def resolve_keyframes(self, map_id, samples, revision=None):
        """Resolve every exact source keyframe against one immutable map revision."""
        with self.store._lock:
            self.store._open()
            info = self.store._map_locked(map_id, revision)
            resolved, missing = [], []
            for original in samples:
                sample = _copy(original)
                row = self.store._db.execute(
                    "SELECT * FROM entity_versions WHERE map_id=? AND kind='keyframe' "
                    "AND entity_id=? AND revision<=? ORDER BY revision DESC LIMIT 1",
                    (map_id, sample["keyframe_id"], info["revision"])).fetchone()
                data = json.loads(row["payload"]) if row and not row["deleted"] else None
                if not data or data.get("source_frame_id") != sample["frame_id"]:
                    missing.append(sample["observation_id"])
                    continue
                sample["T_map_camera"] = data.get("T_map_camera")
                sample["resolved_map_revision"] = info["revision"]
                sample["anchor_revision"] = row["revision"]
                resolved.append(sample)
            return info, resolved, missing

    def save_attachment(self, session, observations, candidate):
        existing = self.attachment(session["map_id"], session_id=session["session_id"])
        if existing:
            return existing
        attachment = {
            "attachment_id": str(uuid.uuid4()), "format_version": 1,
            "session_id": session["session_id"], "map_id": session["map_id"],
            "calibration_id": session["calibration_id"], "created_at": _now(),
            "session": _copy(session), "observations": _copy(observations),
            "applied_solution": _copy(candidate),
            "coordinate_frame": "map_metric", "axes": "same_as_native_map",
            "origin": "native_map_origin", "units": "m",
            "floor_alignment": "unmeasured", "pose_origin": "camera_optical",
        }
        applied = {**session, "state": "applied",
                   "attachment_id": attachment["attachment_id"], "updated_at": _now()}
        with self.store._lock:
            with self.store._transaction():
                self.store._db.execute("INSERT INTO metric_attachments VALUES(?,?,?,?,?)",
                    (attachment["attachment_id"], session["session_id"], session["map_id"],
                     attachment["created_at"], _dump(attachment)))
                self.store._db.execute("UPDATE metric_sessions SET payload=? WHERE id=?",
                                       (_dump(applied), session["session_id"]))
        return attachment

    def attachment(self, map_id, attachment_id=None, session_id=None):
        with self.store._lock:
            self.store._open()
            sql, args = "SELECT payload FROM metric_attachments WHERE map_id=?", [map_id]
            if attachment_id is not None:
                sql += " AND id=?"
                args.append(attachment_id)
            if session_id is not None:
                sql += " AND session_id=?"
                args.append(session_id)
            row = self.store._db.execute(
                sql + " ORDER BY created_at DESC,id DESC LIMIT 1", args).fetchone()
            return json.loads(row[0]) if row else None
