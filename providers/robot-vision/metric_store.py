"""Metric-calibration persistence in the vision store's checkpointed database.

This extension deliberately shares VisionStore's lock, transaction and SQLite
connection so an ordinary map checkpoint includes calibration sessions, exact
frame evidence and applied references. It never changes native map coordinates.
"""
from __future__ import annotations

import json
import math
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

    def resolve_floor_anchors(self, floor, snapshot):
        """Prove a floor reference's supporting native keyframes never changed.

        Exact indexed entity-version identities avoid scanning action/map history.
        A changed row, even if it later returns to the same pose, needs explicit
        requalification rather than an inferred continuation.
        """
        from navigation_frames import identity, rigid_transform

        anchors = floor.get("supporting_keyframes")
        if not isinstance(anchors, list) or not 1 <= len(anchors) <= 16:
            raise ValueError("floor_anchor_records_required")
        source_revision, target_revision = floor.get("map_revision"), snapshot.get("map_revision")
        if (type(source_revision) is not int or type(target_revision) is not int
                or source_revision < 0 or source_revision > target_revision
                or floor.get("map_id") != snapshot.get("map_id")
                or floor.get("calibration_id") != snapshot.get("calibration_id")):
            raise ValueError("floor_anchor_revision_or_calibration_mismatch")
        with self.store._lock:
            self.store._open()
            try:
                source = self.store._map_locked(snapshot["map_id"], source_revision)
                target = self.store._map_locked(snapshot["map_id"], target_revision)
            except (ValueError, KeyError):
                raise ValueError("floor_anchor_map_revision_unavailable") from None
            if any(info.get("metadata", {}).get("calibration_id") != snapshot["calibration_id"]
                   for info in (source, target)):
                raise ValueError("floor_anchor_map_calibration_mismatch")
            proof, seen = [], set()
            sql = ("SELECT revision,deleted,payload FROM entity_versions "
                   "WHERE map_id=? AND kind='keyframe' AND entity_id=? AND revision<=? "
                   "ORDER BY revision DESC LIMIT 1")
            for anchor in anchors:
                if not isinstance(anchor, dict):
                    raise ValueError("floor_anchor_record_invalid")
                keyframe, frame = anchor.get("keyframe_id"), anchor.get("source_frame_id")
                if not identity(keyframe) or not identity(frame) or keyframe in seen:
                    raise ValueError("floor_anchor_identity_invalid")
                seen.add(keyframe)
                pose = rigid_transform(anchor.get("T_map_camera"))
                original = self.store._db.execute(
                    sql, (snapshot["map_id"], keyframe, source_revision)).fetchone()
                current = self.store._db.execute(
                    sql, (snapshot["map_id"], keyframe, target_revision)).fetchone()
                if original is None or current is None or original["deleted"] or current["deleted"]:
                    raise ValueError("floor_anchor_missing_or_deleted")
                if current["revision"] != original["revision"]:
                    raise ValueError("floor_anchor_version_changed")
                data = json.loads(original["payload"])
                if data.get("source_frame_id") != frame or data.get("T_map_camera") != pose:
                    raise ValueError("floor_anchor_source_binding_mismatch")
                proof.append({"keyframe_id": keyframe, "source_frame_id": frame,
                              "native_entity_revision": original["revision"]})
            return {"qualified_map_revision": source_revision,
                    "resolved_map_revision": target_revision,
                    "rule": "identical_native_keyframe_versions", "anchors": proof}

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

    def resolve_keyframes_for_fit(self, map_id, samples, revision=None, *, exclude_deleted=False):
        """Exclude only proven deleted anchor incarnations from checkerboard fits."""
        with self.store._lock:
            info, resolved, missing = self.resolve_keyframes(map_id, samples, revision)
            if not exclude_deleted or not missing:
                return info, resolved, missing, []
            unresolved, excluded, excluded_ids = set(missing), [], set()
            counts = {}
            for sample in samples:
                oid = sample.get("observation_id")
                counts[oid] = counts.get(oid, 0) + 1
            latest_sql = (
                "SELECT * FROM entity_versions WHERE map_id=? AND kind='keyframe' "
                "AND entity_id=? AND revision<=? ORDER BY revision DESC LIMIT 1")

            def same_image(row, frame_id):
                if row is None or row["deleted"]:
                    return False
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError):
                    return False
                if not isinstance(payload, dict) or payload.get("source_frame_id") != frame_id:
                    return False
                transform = payload.get("T_map_camera")
                return (isinstance(transform, list) and len(transform) == 4 and
                        all(isinstance(axis, list) and len(axis) == 4 and
                            all(not isinstance(v, bool) and isinstance(v, (int, float)) and
                                math.isfinite(v) for v in axis) for axis in transform))

            for sample in samples:
                oid = sample.get("observation_id")
                if oid not in unresolved or counts[oid] != 1:
                    continue
                source_revision, frame_id = sample.get("source_map_revision"), sample.get("frame_id")
                calibration_id = sample.get("calibration_id")
                if (sample.get("map_id") != map_id or not frame_id or not calibration_id or
                        calibration_id != info["metadata"].get("calibration_id") or
                        type(source_revision) is not int or source_revision < 0 or
                        source_revision > info["revision"]):
                    continue
                keyframe_id = sample["keyframe_id"]
                deletion = self.store._db.execute(
                    latest_sql, (map_id, keyframe_id, info["revision"])).fetchone()
                if (deletion is None or not deletion["deleted"] or
                        deletion["revision"] <= source_revision):
                    continue
                try:
                    source_map = self.store._map_locked(map_id, source_revision)
                except (KeyError, ValueError):
                    continue
                if source_map["metadata"].get("calibration_id") != calibration_id:
                    continue
                original = self.store._db.execute(
                    latest_sql, (map_id, keyframe_id, source_revision)).fetchone()
                if not same_image(original, frame_id):
                    continue
                history = self.store._db.execute(
                    "SELECT deleted,payload FROM entity_versions WHERE map_id=? AND kind='keyframe' "
                    "AND entity_id=? AND revision>? AND revision<? ORDER BY revision",
                    (map_id, keyframe_id, source_revision, deletion["revision"])).fetchall()
                if any(not same_image(row, frame_id) for row in history):
                    continue
                excluded_ids.add(oid)
                excluded.append({
                    "observation_id": oid, "keyframe_id": keyframe_id,
                    "source_map_revision": source_revision, "deletion_revision": deletion["revision"],
                    "reason": "anchor_deleted", "role": sample.get("role"),
                })
            return info, resolved, [oid for oid in missing if oid not in excluded_ids], excluded

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
