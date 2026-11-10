"""Bounded adaptive radio fingerprints anchored to independent visual geometry.

RSSI is evidence about a previously observed region, never a measured range.
The model retains recent and temporally distributed training records, rebuilds
time-decayed distributions from that bounded history, and resolves keyframe
anchors against current geometry on every read. Scores are heuristics, not
calibrated probabilities. Radio output cannot make a visual pose valid.

Input observation times and visual timestamps must share the caller's host
monotonic clock. These are receive-time estimates when capture timing is
unavailable. time_uncertainty_s must include known transport/alignment error.
"""
from __future__ import annotations

import base64
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

DEFAULT_CONFIG = {
    "max_fingerprint_span_s": 1.0,
    "max_pose_time_delta_s": 0.5,
    "max_source_age_s": 15.0,
    "min_sources": 3,
    "min_overlap": 3,
    "min_query_coverage": 0.5,
    "noise_floor_db": 6.0,
    "min_similarity": 0.15,
    "ambiguity_ratio": 0.8,
    "cluster_radius_map_units": 1.0,
    "spatial_bin_size_map_units": 0.5,
    "independent_sample_interval_s": 1.0,
    "min_source_reliability": 0.15,
    "max_candidates": 12,
    "min_anchor_samples": 3,
    "max_observations": 96,
    "max_samples": 512,
    "max_samples_per_anchor": 16,
    "max_anchors": 128,
    "max_contexts": 16,
    "max_watermarks": 4096,
    "max_boots_per_receiver": 64,
    "retention_s": 14 * 86400,
    "dedup_retention_s": 30 * 86400,
    "decay_half_life_s": 86400,
    "max_common_offset_db": 6.0,
    "common_offset_penalty_db": 6.0,
    "max_db_bytes": 16 * 1024 * 1024,
}
_INTEGER_LIMITS = {
    "min_sources": (2, 96), "min_overlap": (2, 96),
    "max_candidates": (1, 64), "min_anchor_samples": (1, 1000),
    "max_observations": (2, 96), "max_samples": (4, 100000),
    "max_samples_per_anchor": (2, 1000), "max_anchors": (1, 10000),
    "max_contexts": (1, 256), "max_watermarks": (4, 100000),
    "max_boots_per_receiver": (2, 1024),
    "max_db_bytes": (262144, 268435456),
}
_MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")


class RadioRejected(ValueError):
    def __init__(self, reason, detail=None):
        super().__init__(detail or reason)
        self.reason = reason


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(name + " must be a finite number")
    if not math.isfinite(value):
        raise ValueError(name + " must be finite")
    return float(value)


def _identifier(value, name):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise ValueError(name + " must be a nonempty bounded identifier")
    if any(ord(c) < 32 for c in value):
        raise ValueError(name + " contains control characters")
    return value


def _integer(value, name, low=0, high=0xffffffff):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(name + " is outside its integer range")
    return value


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def validate_config(config=None):
    config = {} if config is None else config
    if not isinstance(config, dict) or set(config) - set(DEFAULT_CONFIG):
        raise ValueError("Unknown radio mapping configuration")
    result = dict(DEFAULT_CONFIG, **config)
    for key, value in result.items():
        if key in _INTEGER_LIMITS:
            _integer(value, key, *_INTEGER_LIMITS[key])
        else:
            value = _number(value, key)
            if value <= 0 and key != "max_common_offset_db":
                raise ValueError(key + " must be positive")
            if key == "max_common_offset_db" and not 0 <= value <= 30:
                raise ValueError("max_common_offset_db must be between 0 and 30")
            result[key] = value
    for key in ("min_query_coverage", "min_similarity", "ambiguity_ratio", "min_source_reliability"):
        if not 0 < result[key] <= 1:
            raise ValueError(key + " must be in (0, 1]")
    if result["min_sources"] > result["max_observations"]:
        raise ValueError("min_sources exceeds the observation bound")
    if result["min_overlap"] > result["max_observations"]:
        raise ValueError("min_overlap exceeds the observation bound")
    if result["max_anchors"] > result["max_samples"]:
        raise ValueError("max_anchors must not exceed max_samples")
    if result["dedup_retention_s"] < max(300, result["retention_s"]):
        raise ValueError("Deduplication retention must cover model retention and firmware TTL")
    return result


def _identity(row):
    radio = row.get("radio")
    address = str(row.get("address", row.get("bssid", ""))).lower()
    if not _MAC.fullmatch(address):
        raise ValueError("A real transmitter MAC address is required")
    if radio == "wifi":
        identity = "wifi:" + address
        return identity, identity, {
            "radio": radio, "address": address,
            "identity_stability": "bssid_not_a_stationarity_claim",
        }
    if radio != "ble":
        raise ValueError("Radio must be wifi or ble")
    address_type = _integer(row.get("address_type"), "address_type", 0, 3)
    physical = "ble:%d:%s" % (address_type, address)
    metadata = {"radio": radio, "address": address, "address_type": address_type,
                "identity_stability": ("public_address" if address_type in (0, 2)
                                       else "private_or_random_address")}
    beacon = row.get("ibeacon")
    if isinstance(beacon, dict):
        beacon_id = str(uuid.UUID(str(beacon.get("uuid", ""))))
        major = _integer(beacon.get("major"), "ibeacon major", 0, 65535)
        minor = _integer(beacon.get("minor"), "ibeacon minor", 0, 65535)
        identity = "ble:ibeacon:%s:%d:%d" % (beacon_id, major, minor)
        metadata.update(ibeacon={"uuid": beacon_id, "major": major, "minor": minor},
                        identity_stability="advertised_beacon_id_not_authenticated")
        return identity, physical, metadata
    return physical, physical, metadata


def source_identity(observation):
    """Return an RF identity; names/SSIDs, range and TX-power fields are ignored."""
    return _identity(observation)[0]


def _matrix(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Anchor requires a finite rigid 4x4 transform")
    rows = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            raise ValueError("Anchor requires a finite rigid 4x4 transform")
        rows.append([_number(x, "transform element") for x in row])
    if any(abs(rows[3][i] - (1 if i == 3 else 0)) > 1e-6 for i in range(4)):
        raise ValueError("Invalid homogeneous transform")
    for i in range(3):
        for j in range(3):
            dot = sum(rows[k][i] * rows[k][j] for k in range(3))
            if abs(dot - (1 if i == j else 0)) > 1e-3:
                raise ValueError("Anchor rotation must be orthonormal")
    r = rows
    determinant = (r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
                   - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
                   + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]))
    if abs(determinant - 1) > 1e-3:
        raise ValueError("Anchor rotation must preserve handedness")
    return rows


def _distance(a, b):
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)))


def _weighted_median(values):
    values = sorted(values)
    half = sum(weight for _, weight in values) / 2
    total = 0.0
    for value, weight in values:
        total += weight
        if total >= half:
            return value
    return values[-1][0]


def _moments(values):
    """Weighted scalar statistics; each retained RF event contributes one vote."""
    weight = sum(w for _, w in values)
    mean = sum(x*w for x, w in values) / weight
    variance = sum(w*(x-mean)**2 for x, w in values) / weight
    effective = weight**2 / sum(w*w for _, w in values)
    return mean, variance, weight, effective


class RadioMap:
    def __init__(self, path, config=None, *, clock=None):
        self.config = validate_config(config)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=3000")
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA auto_vacuum=FULL")
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        pages = self.config["max_db_bytes"] // page_size
        actual = self._db.execute("PRAGMA max_page_count=%d" % pages).fetchone()[0]
        if actual > pages:
            self._db.close()
            raise ValueError("Existing radio database exceeds configured storage bound")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self._db.close()
            raise ValueError("Unsupported radio database schema")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('revision',0);
            CREATE TABLE IF NOT EXISTS contexts(
                id TEXT PRIMARY KEY,map_id TEXT NOT NULL,calibration_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,receiver_calibration_id TEXT NOT NULL,
                last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS samples(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id TEXT NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
                anchor_id TEXT NOT NULL,anchor_source_frame_id TEXT NOT NULL,
                spatial_cell TEXT NOT NULL,
                frame_id TEXT NOT NULL,observed_utc REAL NOT NULL,
                relative_position TEXT NOT NULL,sources TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_anchor ON samples(context_id,anchor_id);
            CREATE TABLE IF NOT EXISTS watermarks(
                receiver_key TEXT NOT NULL,boot_id TEXT NOT NULL,physical_id TEXT NOT NULL,
                uptime INTEGER NOT NULL,sample_count INTEGER NOT NULL,last_seen REAL NOT NULL,
                PRIMARY KEY(receiver_key,boot_id,physical_id));
            CREATE TABLE IF NOT EXISTS boots(
                receiver_key TEXT NOT NULL,boot_id TEXT NOT NULL,retired INTEGER NOT NULL,
                last_seen REAL NOT NULL,PRIMARY KEY(receiver_key,boot_id));
            PRAGMA user_version=2;
        """)
        self._cell_size = min(self.config["spatial_bin_size_map_units"],
                              self.config["cluster_radius_map_units"]/math.sqrt(3))
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(samples)")}
        with self._db:
            if "spatial_cell" not in columns:
                self._db.execute("ALTER TABLE samples ADD COLUMN spatial_cell TEXT NOT NULL DEFAULT ''")
            previous = self._db.execute("SELECT value FROM meta WHERE key='cell_size'").fetchone()
            if previous is None or previous[0] != self._cell_size:
                for row in self._db.execute("SELECT id,relative_position FROM samples").fetchall():
                    self._db.execute("UPDATE samples SET spatial_cell=? WHERE id=?",
                                     (self._cell(json.loads(row["relative_position"])), row["id"]))
                self._db.execute("INSERT INTO meta VALUES('cell_size',?) ON CONFLICT(key) "
                                 "DO UPDATE SET value=excluded.value", (self._cell_size,))
                self._bump()
        self._db.commit()

    def _cell(self, position):
        return _dump([math.floor(x/self._cell_size) for x in position])

    def _revision(self):
        return self._db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]

    def _bump(self):
        self._db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def _fingerprint(self, fingerprint, now_s):
        now_s = _number(now_s, "now_s")
        if not isinstance(fingerprint, dict):
            raise RadioRejected("invalid_fingerprint")
        receiver = _identifier(fingerprint.get("receiver_id"), "receiver_id")
        boot = _identifier(fingerprint.get("boot_id"), "boot_id")
        calibration = _identifier(fingerprint.get("receiver_calibration_id", "rssi-v1"),
                                  "receiver_calibration_id")
        if fingerprint.get("paused") is True:
            raise RadioRejected("radio_paused")
        observations = fingerprint.get("observations")
        if not isinstance(observations, list) or len(observations) > self.config["max_observations"]:
            raise RadioRejected("invalid_observation_count")
        selected, rejected = {}, defaultdict(int)
        for row in observations:
            try:
                if not isinstance(row, dict):
                    raise ValueError("Observation is not an object")
                identity, physical, metadata = _identity(row)
                rssi = _number(row.get("rssi_dbm"), "rssi_dbm")
                if not -127 <= rssi < 0:
                    raise ValueError("Invalid RSSI")
                observed = _number(row.get("observed_at_s"), "observed_at_s")
                uncertainty = _number(row.get("time_uncertainty_s", 0), "time_uncertainty_s")
                if uncertainty < 0:
                    raise ValueError("Negative time uncertainty")
                age = now_s - observed
                if age < -max(uncertainty, 0.05):
                    rejected["future"] += 1
                    continue
                if max(0, age) + uncertainty > self.config["max_source_age_s"]:
                    rejected["stale"] += 1
                    continue
                uptime = _integer(row.get("observed_uptime_ms"), "observed_uptime_ms")
                count = _integer(row.get("sample_count"), "sample_count", 1)
                if row.get("boot_id", boot) != boot:
                    raise ValueError("Mixed boot identities")
                item = dict(source_id=identity, physical_id=physical, metadata=metadata,
                            rssi=rssi, observed_at_s=observed, uncertainty=uncertainty,
                            age_s=max(0, age), uptime=uptime, sample_count=count)
                previous = selected.get(identity)
                if previous is None or (observed, count) > (previous["observed_at_s"],
                                                            previous["sample_count"]):
                    selected[identity] = item
            except (ValueError, TypeError, AttributeError):
                rejected["invalid"] += 1
        rows = [selected[key] for key in sorted(selected)]
        if len(rows) < self.config["min_sources"]:
            raise RadioRejected("no_fresh_radio" if not rows else "insufficient_sources")
        span = max(r["observed_at_s"] for r in rows) - min(r["observed_at_s"] for r in rows)
        if span > self.config["max_fingerprint_span_s"]:
            raise RadioRejected("incoherent_fingerprint")
        return {"receiver_id": receiver, "boot_id": boot,
                "receiver_calibration_id": calibration,
                "receiver_key": _hash([receiver, calibration]), "rows": rows,
                "span_s": span, "rejected_observations": dict(rejected)}

    def _boot_rejected(self, fp):
        row = self._db.execute(
            "SELECT retired FROM boots WHERE receiver_key=? AND boot_id=?",
            (fp["receiver_key"], fp["boot_id"])).fetchone()
        return row is not None and bool(row["retired"])

    @staticmethod
    def _retained_ids(rows, maximum):
        """Keep recent half and older observations spread over time, not just FIFO."""
        rows = sorted(rows, key=lambda row: (row["observed_utc"], row["id"]))
        if len(rows) <= maximum:
            return {row["id"] for row in rows}
        if maximum == 1:
            return {rows[-1]["id"]}
        recent_count = max(1, maximum // 2)
        recent, history = rows[-recent_count:], rows[:-recent_count]
        selected = [history[0]] if history else []
        if maximum - recent_count > 1 and len(history) > 1:
            selected.append(history[-1])
        while len(selected) < maximum - recent_count:
            remaining = [r for r in history if r not in selected]
            if not remaining:
                break
            selected.append(max(remaining, key=lambda r: (
                min(abs(r["observed_utc"]-s["observed_utc"]) for s in selected),
                r["observed_utc"], r["id"])))
        return {row["id"] for row in selected + recent}

    @classmethod
    def _diverse_retained_ids(cls, rows, maximum):
        """Allocate history across spatial cells before thinning within a cell."""
        groups = defaultdict(list)
        for row in rows:
            groups[row["spatial_cell"]].append(row)
        ordered = sorted(groups.values(), key=lambda group: (
            max(r["observed_utc"] for r in group), max(r["id"] for r in group)), reverse=True)
        retained = ordered[:maximum]
        while sum(len(group) for group in retained) > maximum:
            index = max(range(len(retained)), key=lambda i: len(retained[i]))
            group = retained[index]
            keep = cls._retained_ids(group, len(group)-1)
            retained[index] = [row for row in group if row["id"] in keep]
        return {row["id"] for group in retained for row in group}

    def _prune_locked(self, now):
        before = self._db.total_changes
        cutoff = now - self.config["retention_s"]
        self._db.execute("DELETE FROM samples WHERE observed_utc<?", (cutoff,))
        self._db.execute("DELETE FROM watermarks WHERE last_seen<?",
                         (now-self.config["dedup_retention_s"],))
        self._db.execute("DELETE FROM boots WHERE last_seen<?",
                         (now-self.config["dedup_retention_s"],))
        groups = self._db.execute(
            "SELECT context_id,anchor_id,MAX(observed_utc) last_seen FROM samples "
            "GROUP BY context_id,anchor_id ORDER BY last_seen DESC,context_id,anchor_id").fetchall()
        for group in groups[self.config["max_anchors"]:]:
            self._db.execute("DELETE FROM samples WHERE context_id=? AND anchor_id=?",
                             (group["context_id"], group["anchor_id"]))
        groups = groups[:self.config["max_anchors"]]
        for group in groups:
            rows = self._db.execute(
                "SELECT id,observed_utc,spatial_cell FROM samples WHERE context_id=? AND anchor_id=?",
                (group["context_id"], group["anchor_id"])).fetchall()
            keep = self._diverse_retained_ids(rows, self.config["max_samples_per_anchor"])
            self._db.executemany("DELETE FROM samples WHERE id=?",
                                 [(row["id"],) for row in rows if row["id"] not in keep])
        # Share global capacity across locations. Preserve a representative at every
        # retained anchor rather than letting a frequently visited location evict all
        # quiet locations. The explicit anchor cap controls eventual coverage loss.
        while self._db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] > self.config["max_samples"]:
            group = self._db.execute(
                "SELECT context_id,anchor_id,spatial_cell,COUNT(*) n FROM samples "
                "GROUP BY context_id,anchor_id,spatial_cell HAVING COUNT(*)>1 "
                "ORDER BY n DESC,context_id,anchor_id,spatial_cell LIMIT 1").fetchone()
            if group is None:
                self._db.execute("DELETE FROM samples WHERE id=(SELECT id FROM samples "
                                 "ORDER BY observed_utc,id LIMIT 1)")
                continue
            rows = self._db.execute(
                "SELECT id,observed_utc FROM samples WHERE context_id=? AND anchor_id=? AND spatial_cell=?",
                (group["context_id"], group["anchor_id"], group["spatial_cell"])).fetchall()
            keep = self._retained_ids(rows, len(rows)-1)
            self._db.executemany("DELETE FROM samples WHERE id=?",
                                 [(row["id"],) for row in rows if row["id"] not in keep])
        contexts = self._db.execute(
            "SELECT id FROM contexts ORDER BY last_seen DESC,id").fetchall()
        self._db.executemany("DELETE FROM contexts WHERE id=?",
                             [(r["id"],) for r in contexts[self.config["max_contexts"]:]])
        self._db.execute("DELETE FROM contexts WHERE id NOT IN (SELECT context_id FROM samples)")
        changed = self._db.total_changes - before
        if changed:
            self._bump()
        return changed

    def train(self, map_id, calibration_id, fingerprint, visual_pose, anchor, *,
              now_s, now_utc_s=None):
        """Train once from fresh RF events and independently validated visual geometry."""
        now = _number(self._clock() if now_utc_s is None else now_utc_s, "now_utc_s")
        try:
            map_id = _identifier(map_id, "map_id")
            calibration_id = _identifier(calibration_id, "calibration_id")
            fp = self._fingerprint(fingerprint, now_s)
            if not isinstance(visual_pose, dict) or not (
                visual_pose.get("valid") is True
                and visual_pose.get("source") == "visual"
                and visual_pose.get("independently_validated") is True
            ):
                raise RadioRejected("independent_visual_pose_required")
            if (visual_pose.get("map_id") != map_id or
                    visual_pose.get("calibration_id") != calibration_id):
                raise RadioRejected("visual_context_mismatch")
            timestamp = _number(visual_pose.get("timestamp_s"), "visual timestamp")
            frame = _identifier(visual_pose.get("source_frame_id"), "source_frame_id")
            if any(abs(r["observed_at_s"]-timestamp)+r["uncertainty"] >
                   self.config["max_pose_time_delta_s"] for r in fp["rows"]):
                raise RadioRejected("visual_radio_time_mismatch")
            if not isinstance(anchor, dict) or anchor.get("is_bad") or anchor.get("culled"):
                raise RadioRejected("valid_keyframe_anchor_required")
            anchor_id = _identifier(anchor.get("keyframe_id"), "keyframe_id")
            anchor_source = _identifier(anchor.get("source_frame_id"), "anchor source_frame_id")
            if (visual_pose.get("reference_keyframe_id") is not None
                    and str(visual_pose["reference_keyframe_id"]) != anchor_id):
                raise RadioRejected("visual_anchor_mismatch")
            transform = _matrix(anchor.get("T_keyframe_camera"))
            relative = [transform[i][3] for i in range(3)]
        except RadioRejected as exc:
            return {"accepted": False, "reason": exc.reason}
        except (ValueError, TypeError, AttributeError) as exc:
            return {"accepted": False, "reason": "invalid_training_input", "detail": str(exc)}
        context = _hash([map_id, calibration_id, fp["receiver_id"],
                         fp["receiver_calibration_id"]])
        with self._lock:
            try:
                with self._db:
                    self._prune_locked(now)
                    if self._boot_rejected(fp):
                        return {"accepted": False, "reason": "retired_boot"}
                    rows, duplicate, added_keys = [], 0, 0
                    for row in fp["rows"]:
                        previous = self._db.execute(
                            "SELECT uptime,sample_count FROM watermarks "
                            "WHERE receiver_key=? AND boot_id=? AND physical_id=?",
                            (fp["receiver_key"], fp["boot_id"], row["physical_id"])).fetchone()
                        newer = previous is None
                        if previous is not None:
                            delta = (row["uptime"]-previous["uptime"]) & 0xffffffff
                            newer = (0 < delta < 0x80000000 or
                                     (delta == 0 and row["sample_count"] > previous["sample_count"]))
                        if newer:
                            rows.append(row)
                            added_keys += previous is None
                        else:
                            duplicate += 1
                    if len(rows) < self.config["min_sources"]:
                        return {"accepted": False,
                                "reason": "replayed_observation" if not rows else "insufficient_new_sources",
                                "new_sources": len(rows), "duplicate_sources": duplicate}
                    if self._db.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0] + added_keys > self.config["max_watermarks"]:
                        return {"accepted": False, "reason": "dedup_capacity"}
                    known_boot = self._db.execute(
                        "SELECT 1 FROM boots WHERE receiver_key=? AND boot_id=?",
                        (fp["receiver_key"], fp["boot_id"])).fetchone()
                    if not known_boot and self._db.execute(
                        "SELECT COUNT(*) FROM boots WHERE receiver_key=?",
                        (fp["receiver_key"],)).fetchone()[0] >= self.config["max_boots_per_receiver"]:
                        return {"accepted": False, "reason": "boot_history_capacity"}
                    self._db.execute("UPDATE boots SET retired=1 WHERE receiver_key=? AND boot_id<>?",
                                     (fp["receiver_key"], fp["boot_id"]))
                    self._db.execute(
                        "INSERT INTO boots VALUES(?,?,0,?) ON CONFLICT(receiver_key,boot_id) "
                        "DO UPDATE SET last_seen=excluded.last_seen",
                        (fp["receiver_key"], fp["boot_id"], now))
                    sources = {}
                    for row in rows:
                        observed_utc = now - max(0, now_s-row["observed_at_s"])
                        sources[row["source_id"]] = dict(row["metadata"], rssi_dbm=row["rssi"],
                                                        observed_utc=observed_utc)
                        self._db.execute(
                            "INSERT INTO watermarks VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(receiver_key,boot_id,physical_id) DO UPDATE SET "
                            "uptime=excluded.uptime,sample_count=excluded.sample_count,last_seen=excluded.last_seen",
                            (fp["receiver_key"], fp["boot_id"], row["physical_id"],
                             row["uptime"], row["sample_count"], now))
                    self._db.execute(
                        "INSERT INTO contexts VALUES(?,?,?,?,?,?) ON CONFLICT(id) "
                        "DO UPDATE SET last_seen=excluded.last_seen",
                        (context, map_id, calibration_id, fp["receiver_id"],
                         fp["receiver_calibration_id"], now))
                    observed = min(s["observed_utc"] for s in sources.values())
                    # A restored map can reuse a numeric native ID for a different
                    # capture. Never combine or rebind those anchor incarnations.
                    self._db.execute("DELETE FROM samples WHERE context_id=? AND anchor_id=? "
                                     "AND anchor_source_frame_id<>?", (context, anchor_id, anchor_source))
                    cursor = self._db.execute(
                        "INSERT INTO samples(context_id,anchor_id,anchor_source_frame_id,spatial_cell,frame_id,observed_utc,relative_position,sources)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (context, anchor_id, anchor_source, self._cell(relative), frame, observed, _dump(relative), _dump(sources)))
                    sample_id = cursor.lastrowid
                    self._bump()
                    self._prune_locked(now)
                    return {"accepted": True, "reason": "trained", "new_sources": len(rows),
                            "duplicate_sources": duplicate, "fingerprint_id": sample_id,
                            "revision": self._revision(),
                            "timing_basis": "host_receive_time_estimate",
                            "retained": self._db.execute("SELECT 1 FROM samples WHERE id=?",
                                                         (sample_id,)).fetchone() is not None}
            except sqlite3.OperationalError as exc:
                if "full" in str(exc).lower():
                    return {"accepted": False, "reason": "storage_capacity"}
                raise

    def _models(self, map_id, calibration_id, now, receiver=None, receiver_calibration=None):
        sql = ("SELECT samples.*,contexts.receiver_id,contexts.receiver_calibration_id "
               "FROM samples JOIN contexts ON contexts.id=samples.context_id "
               "WHERE contexts.map_id=? AND contexts.calibration_id=?")
        args = [map_id, calibration_id]
        if receiver is not None:
            sql += " AND contexts.receiver_id=? AND contexts.receiver_calibration_id=?"
            args += [receiver, receiver_calibration]
        groups = defaultdict(list)
        for row in self._db.execute(sql, args):
            groups[(row["context_id"], row["anchor_id"], row["spatial_cell"])].append(row)
        models = []
        for (context_id, anchor_id, cell), rows in sorted(groups.items()):
            source_values, source_meta, last_seen = defaultdict(list), {}, {}
            source_frames, source_epochs = defaultdict(set), defaultdict(set)
            rows = sorted(rows, key=lambda row: (row["observed_utc"], row["id"]))
            epoch_for, epoch_counts, frame_counts = {}, defaultdict(int), defaultdict(int)
            epoch, epoch_start = -1, None
            for row in rows:
                if epoch_start is None or row["observed_utc"]-epoch_start >= self.config["independent_sample_interval_s"]:
                    epoch += 1
                    epoch_start = row["observed_utc"]
                epoch_for[row["id"]] = epoch
                epoch_counts[epoch] += 1
                frame_counts[row["frame_id"]] += 1
            positions = []
            for row in rows:
                # Repeats retain variance information without multiplying pose support.
                divisor = max(epoch_counts[epoch_for[row["id"]]], frame_counts[row["frame_id"]])
                weight = 2 ** (-max(0, now-row["observed_utc"]) / self.config["decay_half_life_s"]) / divisor
                positions.append((json.loads(row["relative_position"]), weight))
                for identity, source in json.loads(row["sources"]).items():
                    source_weight = 2 ** (-max(0, now-source["observed_utc"]) / self.config["decay_half_life_s"]) / divisor
                    source_values[identity].append((source["rssi_dbm"], source_weight))
                    source_frames[identity].add(row["frame_id"])
                    source_epochs[identity].add(epoch_for[row["id"]])
                    if source["observed_utc"] >= last_seen.get(identity, -float("inf")):
                        source_meta[identity] = {k:v for k,v in source.items()
                                                 if k not in ("rssi_dbm", "observed_utc")}
                        last_seen[identity] = source["observed_utc"]
            if sum(w for _,w in positions) < 1e-12:
                continue
            sources = {}
            floor2 = self.config["noise_floor_db"]**2
            for identity, values in source_values.items():
                if sum(w for _,w in values) < 1e-12:
                    continue
                mean, variance, weight, effective = _moments(values)
                reliability = weight/(weight+2.0) * floor2/(floor2+variance)
                sources[identity] = dict(
                    source_meta[identity], source_id=identity, mean_rssi_dbm=mean,
                    variance_db2=variance, sample_count=len(values),
                    effective_weight=weight,
                    effective_sample_count=min(effective, len(source_frames[identity]), len(source_epochs[identity])),
                    visual_frame_count=len(source_frames[identity]), sampling_epoch_count=len(source_epochs[identity]),
                    reliability=reliability, reliability_basis="variation_within_anchor_and_aged_support",
                    transmitter_motion="not_inferred", last_seen_utc_s=last_seen[identity])
            total = sum(w for _,w in positions)
            position = [sum(p[i]*w for p,w in positions)/total for i in range(3)]
            covariance = [[sum(w*(p[i]-position[i])*(p[j]-position[j])
                               for p,w in positions)/total for j in range(3)] for i in range(3)]
            models.append({"keyframe_id": anchor_id,
                           "region_id": _hash([context_id, anchor_id, rows[0]["anchor_source_frame_id"], self._cell_size, cell])[:24],
                           "spatial_cell": json.loads(cell), "spatial_bin_size_map_units": self._cell_size,
                           "sampling_epoch_count": len(epoch_counts),
                           "anchor_source_frame_id": rows[0]["anchor_source_frame_id"],
                           "receiver_id": rows[0]["receiver_id"],
                           "receiver_calibration_id": rows[0]["receiver_calibration_id"],
                           "relative_position": position, "relative_covariance": covariance,
                           "sources": sources, "sample_count": len(rows),
                           "visual_frame_count": len({r["frame_id"] for r in rows}),
                           "effective_weight": total,
                           "last_seen_utc_s": max(r["observed_utc"] for r in rows)})
        return models

    def _maturity(self, model, source_ids=None):
        source_ids = model["sources"] if source_ids is None else source_ids
        result = {
            "enough_visual_frames": model["visual_frame_count"] >= self.config["min_anchor_samples"],
            "enough_sampling_epochs": model["sampling_epoch_count"] >= self.config["min_anchor_samples"],
            "reliable_source_count": sum(model["sources"][identity]["reliability"] >=
                                         self.config["min_source_reliability"] for identity in source_ids),
        }
        result["ready_for_coarse_inference"] = (
            result["enough_visual_frames"] and result["enough_sampling_epochs"]
            and result["reliable_source_count"] >= self.config["min_overlap"])
        return result

    @staticmethod
    def _resolve(model, keyframes):
        value = keyframes.get(model["keyframe_id"])
        if not isinstance(value, dict) or value.get("source_frame_id") != model["anchor_source_frame_id"]:
            return None
        if isinstance(value, dict):
            if value.get("is_bad") or value.get("_is_bad") or value.get("culled"):
                return None
            matrix, units = value.get("T_map_camera"), value.get("units", "arbitrary")
        else:
            matrix, units = value, "arbitrary"
        try:
            matrix = _matrix(matrix)
        except (ValueError, TypeError):
            return None
        position = [matrix[i][3] + sum(matrix[i][j]*model["relative_position"][j]
                                      for j in range(3)) for i in range(3)]
        covariance = [[sum(matrix[i][a]*model["relative_covariance"][a][b]*matrix[j][b]
                           for a in range(3) for b in range(3)) for j in range(3)] for i in range(3)]
        return {"position": position, "covariance": covariance, "units": units, "matrix": matrix}

    @staticmethod
    def _empty(state, map_id, calibration_id, detail=None):
        result = {"state": state, "map_id": map_id, "calibration_id": calibration_id,
                  "method": "adaptive_rssi_fingerprint", "candidate_keyframe_ids": [],
                  "candidates": [], "clusters": [], "position": None,
                  "valid_visual_pose": False, "heading_available": False,
                  "navigation_usable": False, "metric_position": False,
                  "confidence": {"score": 0.0, "kind": "heuristic_not_probability"},
                  "timing_basis": "host_receive_time_estimate"}
        if detail is not None:
            result["detail"] = detail
        return result

    def estimate(self, map_id, calibration_id, fingerprint, keyframes, *,
                 now_s, now_utc_s=None):
        now = _number(self._clock() if now_utc_s is None else now_utc_s, "now_utc_s")
        try:
            map_id = _identifier(map_id, "map_id")
            calibration_id = _identifier(calibration_id, "calibration_id")
            fp = self._fingerprint(fingerprint, now_s)
        except RadioRejected as exc:
            return self._empty(exc.reason, map_id, calibration_id)
        except (ValueError, TypeError) as exc:
            return self._empty("invalid_fingerprint", map_id, calibration_id, str(exc))
        result = self._empty("no_model", map_id, calibration_id)
        result.update(receiver_id=fp["receiver_id"],
                      receiver_calibration_id=fp["receiver_calibration_id"],
                      observed_time_range_s=[min(r["observed_at_s"] for r in fp["rows"]),
                                             max(r["observed_at_s"] for r in fp["rows"])],
                      max_observation_age_s=max(r["age_s"]+r["uncertainty"] for r in fp["rows"]),
                      rejected_observations=fp["rejected_observations"])
        query = {row["source_id"]: row for row in fp["rows"]}
        candidates, unresolved = [], 0
        with self._lock, self._db:
            self._prune_locked(now)
            result["revision"] = self._revision()
            if self._boot_rejected(fp):
                result["state"] = "retired_boot"
                return result
            models = self._models(map_id, calibration_id, now, fp["receiver_id"],
                                  fp["receiver_calibration_id"])
        for model in models:
            resolved = self._resolve(model, keyframes)
            if resolved is None:
                unresolved += 1
                continue
            common = sorted(set(query) & set(model["sources"]))
            coverage = len(common)/len(query)
            if len(common) < self.config["min_overlap"] or coverage < self.config["min_query_coverage"]:
                continue
            residuals = [(query[s]["rssi"]-model["sources"][s]["mean_rssi_dbm"],
                          max(1e-12, model["sources"][s]["reliability"])) for s in common]
            offset = max(-self.config["max_common_offset_db"],
                         min(self.config["max_common_offset_db"], _weighted_median(residuals)))
            reliability = sum(w for _,w in residuals)
            normalized_error = sum(
                model["sources"][s]["reliability"] *
                (query[s]["rssi"]-model["sources"][s]["mean_rssi_dbm"]-offset)**2 /
                (self.config["noise_floor_db"]**2+model["sources"][s]["variance_db2"])
                for s in common) / max(reliability, 1e-12)
            shift_penalty = (offset/self.config["common_offset_penalty_db"])**2
            similarity = math.exp(-0.5*(normalized_error+shift_penalty))*coverage
            if similarity < self.config["min_similarity"]:
                continue
            mean_reliability = reliability/len(common)
            support = min(1.0, model["effective_weight"]/self.config["min_anchor_samples"])
            candidates.append({
                "keyframe_id": model["keyframe_id"], "region_id": model["region_id"],
                "spatial_cell": model["spatial_cell"], "spatial_bin_size_map_units": self._cell_size,
                "position": resolved["position"],
                "covariance_map_units2": resolved["covariance"], "units": resolved["units"],
                "similarity": similarity, "overlap_count": len(common), "query_coverage": coverage,
                "mean_absolute_error_db": sum(abs(x) for x,_ in residuals)/len(common),
                "adjusted_normalized_error": normalized_error, "common_offset_db": offset,
                "common_offset_penalty": shift_penalty,
                "mean_source_reliability": mean_reliability, "effective_weight": model["effective_weight"],
                "sample_count": model["sample_count"], "visual_frame_count": model["visual_frame_count"],
                "sampling_epoch_count": model["sampling_epoch_count"],
                "maturity": self._maturity(model, common),
                "mature": self._maturity(model, common)["ready_for_coarse_inference"],
                "support_score": similarity*support*mean_reliability/(1+shift_penalty),
                "last_seen_utc_s": model["last_seen_utc_s"],
            })
        result["unresolved_anchor_count"] = unresolved
        if not candidates:
            result["state"] = "insufficient_overlap" if models else "no_model"
            return result
        candidates.sort(key=lambda row: (-row["similarity"], row["keyframe_id"], row["region_id"]))
        # Analyze every retained matching region before limiting returned suggestions.
        # A crowded nearby mode must not hide a distant alias below the display budget.
        result["candidates"] = candidates[:self.config["max_candidates"]]
        result["candidate_keyframe_ids"] = list(dict.fromkeys(
            row["keyframe_id"] for row in candidates))[:self.config["max_candidates"]]
        result["candidate_count"] = len(candidates)
        result["candidates_truncated"] = len(candidates) > self.config["max_candidates"]
        # Complete-link spatial grouping prevents a long chain of weak neighbors
        # from silently joining separated place hypotheses.
        groups = []
        for row in candidates:
            group = next((g for g in groups if all(
                _distance(row["position"], other["position"]) <= self.config["cluster_radius_map_units"]
                and row["units"] == other["units"] for other in g)), None)
            if group is None:
                groups.append([row])
            else:
                group.append(row)
        clusters = []
        for group in groups:
            weight = sum(row["similarity"] for row in group)
            position = [sum(row["similarity"]*row["position"][i] for row in group)/weight
                        for i in range(3)]
            covariance = [[sum(row["similarity"]*(
                row["covariance_map_units2"][i][j] +
                (row["position"][i]-position[i])*(row["position"][j]-position[j]))
                for row in group)/weight for j in range(3)] for i in range(3)]
            radius = max(_distance(row["position"], position) +
                         math.sqrt(max(0, sum(row["covariance_map_units2"][i][i] for i in range(3))))
                         for row in group)
            clusters.append({
                "keyframe_ids": list(dict.fromkeys(row["keyframe_id"] for row in group)),
                "region_ids": [row["region_id"] for row in group],
                "mature": any(row["mature"] for row in group), "position": position,
                "covariance_map_units2": covariance, "spread_map_units": radius,
                "similarity": max(row["similarity"] for row in group),
                "support_score": sum(row["support_score"]*row["similarity"] for row in group)/weight,
                "units": group[0]["units"],
            })
        clusters.sort(key=lambda group: -group["similarity"])
        # Spend the bounded prior on competing places before redundant neighbors.
        # Keep a representative of each leading spatial mode in the display too.
        by_region = {row["region_id"]: row for row in candidates}
        mode_first, prior_ids, displayed_regions = [], [], set()
        for cluster in clusters:
            members = [by_region[region] for region in cluster["region_ids"]]
            representative = next((row for row in members
                                   if row["keyframe_id"] not in prior_ids), members[0])
            if representative["keyframe_id"] not in prior_ids:
                prior_ids.append(representative["keyframe_id"])
            if representative["region_id"] not in displayed_regions:
                mode_first.append(representative)
                displayed_regions.add(representative["region_id"])
        for row in candidates:
            if row["keyframe_id"] not in prior_ids:
                prior_ids.append(row["keyframe_id"])
            if row["region_id"] not in displayed_regions:
                mode_first.append(row)
                displayed_regions.add(row["region_id"])
        result["candidates"] = mode_first[:self.config["max_candidates"]]
        result["candidate_keyframe_ids"] = prior_ids[:self.config["max_candidates"]]
        ratio = clusters[1]["similarity"]/clusters[0]["similarity"] if len(clusters) > 1 else 0.0
        ambiguous = ratio >= self.config["ambiguity_ratio"]
        top = clusters[0]
        abstain = ambiguous or not top["mature"]
        state = "ambiguous" if ambiguous else ("coarse_match" if top["mature"] else "insufficient_model_support")
        result.update(state=state, clusters=clusters[:self.config["max_candidates"]],
                      clusters_truncated=len(clusters) > self.config["max_candidates"],
                      position=None if abstain else top["position"], units=top["units"],
                      overlap={"query_sources": len(query), "best_count": candidates[0]["overlap_count"],
                               "best_query_coverage": candidates[0]["query_coverage"]},
                      disagreement={"separated_mode_count": len(clusters), "runner_up_ratio": ratio,
                                    "ambiguous": ambiguous},
                      uncertainty={"kind": "empirical_conditional_spread_not_accuracy_bound",
                                   "covariance_map_units2": None if abstain else top["covariance_map_units2"],
                                   "radius_map_units": None if abstain else top["spread_map_units"],
                                   "unmodelled_errors": ["multipath", "unobserved_transmitter_change",
                                                        "receiver_orientation", "receive_time_alignment"]},
                      confidence={"score": max(0, min(1, top["support_score"]*(1-ratio if ambiguous else 1))),
                                  "kind": "heuristic_not_probability",
                                  "independent_sources_assumed": False,
                                  "ambiguous": ambiguous})
        return result

    def read_map(self, map_id, calibration_id, keyframes, *, cursor=None, limit=100,
                 source_id=None):
        map_id, calibration_id = _identifier(map_id, "map_id"), _identifier(calibration_id, "calibration_id")
        _integer(limit, "limit", 1, 500)
        if source_id is not None:
            source_id = _identifier(source_id, "source_id")
        now = _number(self._clock(), "clock")
        with self._lock, self._db:
            self._prune_locked(now)
            revision = self._revision()
            models = self._models(map_id, calibration_id, now)
        entries, geometry = [], {}
        for model in models:
            resolved = self._resolve(model, keyframes)
            geometry[model["region_id"]] = {"anchor_source_frame_id": model["anchor_source_frame_id"],
                                             "matrix": None if resolved is None else resolved["matrix"]}
            if resolved is None or (source_id is not None and source_id not in model["sources"]):
                continue
            sources = [value for identity,value in sorted(model["sources"].items())
                       if source_id is None or identity == source_id]
            entries.append({
                "keyframe_id": model["keyframe_id"],
                "anchor_source_frame_id": model["anchor_source_frame_id"],
                "region_id": model["region_id"], "spatial_cell": model["spatial_cell"],
                "spatial_bin_size_map_units": self._cell_size,
                "receiver_id": model["receiver_id"],
                "receiver_calibration_id": model["receiver_calibration_id"],
                "position": resolved["position"], "units": resolved["units"],
                "relative_position": model["relative_position"],
                "covariance_map_units2": resolved["covariance"],
                "sample_count": model["sample_count"], "visual_frame_count": model["visual_frame_count"],
                "sampling_epoch_count": model["sampling_epoch_count"], "maturity": self._maturity(model),
                "effective_weight": model["effective_weight"],
                "last_seen_utc_s": model["last_seen_utc_s"], "sources": sources,
                "timing_basis": "host_receive_time_estimate",
            })
        entries.sort(key=lambda row: (row["receiver_id"], row["receiver_calibration_id"],
                                      row["keyframe_id"], row["region_id"]))
        geometry_revision = _hash(geometry)
        query = _hash([map_id, calibration_id, source_id])
        offset = 0
        # Time-decayed statistics must also remain fixed across a page traversal.
        at = now
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if (value["revision"] != revision or value["geometry"] != geometry_revision
                        or value["query"] != query):
                    raise ValueError("Radio map or geometry changed; restart pagination")
                offset = _integer(value["offset"], "cursor offset", 0, len(entries))
                at = _number(value["at"], "cursor time")
            except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                raise ValueError("Invalid or changed radio map cursor") from exc
            # Recompute at the original page's age so statistics do not drift mid-read.
            with self._lock:
                if self._revision() != revision:
                    raise ValueError("Radio map changed; restart pagination")
                models_at = self._models(map_id, calibration_id, at)
            by_key = {(m["receiver_id"], m["receiver_calibration_id"], m["region_id"]):m for m in models_at}
            for entry in entries:
                model = by_key[(entry["receiver_id"], entry["receiver_calibration_id"], entry["region_id"])]
                resolved = self._resolve(model, keyframes)
                entry.update(position=resolved["position"], relative_position=model["relative_position"],
                             covariance_map_units2=resolved["covariance"],
                             effective_weight=model["effective_weight"], maturity=self._maturity(model),
                             sources=[s for key,s in sorted(model["sources"].items())
                                      if source_id is None or key == source_id])
        page = entries[offset:offset+limit]
        following = offset+len(page)
        next_cursor = None
        if following < len(entries):
            next_cursor = base64.urlsafe_b64encode(_dump(
                {"revision": revision, "geometry": geometry_revision, "query": query,
                 "offset": following, "at": at}).encode()).decode()
        return {"map_id": map_id, "calibration_id": calibration_id, "revision": revision,
                "geometry_revision": geometry_revision, "evaluated_at_utc_s": at,
                "entries": page, "total_entries": len(entries), "next_cursor": next_cursor,
                "history_policy": "recent_plus_temporal_coverage_with_location_diversity",
                "spatial_bin_size_map_units": self._cell_size,
                "decay_half_life_s": self.config["decay_half_life_s"],
                "retention_s": self.config["retention_s"]}

    def prune(self, now_utc_s=None):
        now = _number(self._clock() if now_utc_s is None else now_utc_s, "now_utc_s")
        with self._lock, self._db:
            changes = self._prune_locked(now)
            return {"changes": changes, "revision": self._revision()}

    def clear(self, map_id=None):
        """Clear learned space while preserving replay protection."""
        with self._lock, self._db:
            if map_id is None:
                self._db.execute("DELETE FROM contexts")
            else:
                self._db.execute("DELETE FROM contexts WHERE map_id=?",
                                 (_identifier(map_id, "map_id"),))
            self._bump()
            return {"cleared": True, "revision": self._revision(),
                    "replay_protection_preserved": True}

    def status(self):
        with self._lock:
            counts = {name: self._db.execute("SELECT COUNT(*) FROM "+name).fetchone()[0]
                      for name in ("contexts", "samples", "watermarks", "boots")}
            counts["anchors"] = self._db.execute(
                "SELECT COUNT(*) FROM (SELECT context_id,anchor_id FROM samples GROUP BY context_id,anchor_id)"
            ).fetchone()[0]
            counts["regions"] = self._db.execute(
                "SELECT COUNT(*) FROM (SELECT context_id,anchor_id,spatial_cell FROM samples "
                "GROUP BY context_id,anchor_id,spatial_cell)").fetchone()[0]
            return {"schema_version": 2, "revision": self._revision(), "counts": counts,
                    "spatial_bin_size_map_units": self._cell_size,
                    "storage": {"path": str(self.path), "bytes": self.path.stat().st_size,
                                "max_db_bytes": self.config["max_db_bytes"], "journal_mode": "delete"},
                    "config": dict(self.config),
                    "history_policy": "recent_plus_temporal_coverage_with_location_diversity",
                    "score_semantics": "heuristic_not_probability",
                    "radio_range_used": False}

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
