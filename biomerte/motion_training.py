"""Durable manual-training sessions. Recording never owns physical controls."""
import json
import re
import sqlite3
import time
from pathlib import Path


def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS training_sessions(
      id TEXT PRIMARY KEY,label TEXT,metadata TEXT,started REAL,stopped REAL,
      initial_model TEXT,observed REAL,closed_observed REAL);
    CREATE UNIQUE INDEX IF NOT EXISTS training_one_active
      ON training_sessions((1)) WHERE stopped IS NULL;
    CREATE TABLE IF NOT EXISTS training_events(
      session TEXT,event_id INTEGER PRIMARY KEY,angular INTEGER,metric INTEGER,
      rejections TEXT);
    CREATE INDEX IF NOT EXISTS training_by_session ON training_events(session,event_id);
    CREATE TABLE IF NOT EXISTS training_health(id INTEGER PRIMARY KEY,payload TEXT,updated REAL);
    CREATE TABLE IF NOT EXISTS training_retention(id INTEGER PRIMARY KEY,legacy_max_id INTEGER);
    CREATE INDEX IF NOT EXISTS motion_events_recorded ON events(recorded_utc);
    """)
    # Preserve all pre-existing driving evidence. New unlabelled raw telemetry
    # is a short diagnostic buffer; named session evidence remains retained.
    db.execute("INSERT OR IGNORE INTO training_retention SELECT 1,COALESCE(max(id),0) FROM events")
    db.commit()


def record(db, event_id, received_utc, angular, metric, rejections):
    row=db.execute("SELECT id FROM training_sessions WHERE started<=? AND (stopped IS NULL OR stopped>?) ORDER BY started DESC LIMIT 1",(received_utc,received_utc)).fetchone()
    if row:
        db.execute("INSERT OR IGNORE INTO training_events VALUES(?,?,?,?,?)",
                   (row[0],event_id,angular,metric,json.dumps(rejections)))
        db.execute("UPDATE training_sessions SET observed=? WHERE id=?",(time.time(),row[0]))
    # Heartbeats share the recorder FIFO: acknowledgement after stop means all
    # preceding queued events have been processed, including pre-stop receipts.
    db.execute("UPDATE training_sessions SET closed_observed=? WHERE stopped IS NOT NULL AND stopped<=? AND closed_observed IS NULL",(time.time(),received_utc))


class TrainingStore:
    def __init__(self,database):
        self.database=Path(database)

    def query(self,action,inputs):
        if not isinstance(inputs,dict):raise ValueError("Inputs must be an object")
        allowed={"start":{"session_id","label","metadata"},"stop":{"session_id"},
                 "status":{"session_id"},"read":{"session_id","after_id","limit"}}
        if action not in allowed or set(inputs)-allowed[action]:raise ValueError("Unknown training input fields")
        session=inputs.get("session_id")
        if session is not None and (not isinstance(session,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",session)):
            raise ValueError("session_id must be 1..128 letters, digits, underscores or hyphens")
        if action!="status" and session is None:raise ValueError("session_id is required")
        # Never silently create an empty model database when the observer is absent.
        db=sqlite3.connect(self.database.resolve().as_uri()+"?mode=rw",uri=True,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            schema(db)
            if action=="start":
                label=inputs.get("label",session);metadata=inputs.get("metadata",{})
                if not isinstance(label,str) or len(label)>512 or not isinstance(metadata,dict):
                    raise ValueError("label must be text <=512 characters and metadata an object")
                encoded=json.dumps(metadata,allow_nan=False,sort_keys=True)
                if len(encoded.encode())>16384:raise ValueError("metadata exceeds 16 KiB")
                db.execute("BEGIN IMMEDIATE")
                old=db.execute("SELECT * FROM training_sessions WHERE id=?",(session,)).fetchone()
                if old and (old["label"]!=label or old["metadata"]!=encoded):
                    raise ValueError("session_id already belongs to different session metadata")
                if not old:
                    active=db.execute("SELECT id FROM training_sessions WHERE stopped IS NULL").fetchone()
                    if active:raise ValueError("Another training session is active: "+active["id"])
                    model=db.execute("SELECT payload FROM model WHERE id=1").fetchone()
                    if model is None:raise ValueError("Observer model is unavailable")
                    db.execute("INSERT INTO training_sessions VALUES(?,?,?,?,NULL,?,NULL,NULL)",
                               (session,label,encoded,time.time(),model[0]))
                db.commit()
            elif action=="stop":
                db.execute("UPDATE training_sessions SET stopped=COALESCE(stopped,?) WHERE id=?",(time.time(),session))
                db.commit()
            if session is None:
                row=db.execute("SELECT * FROM training_sessions ORDER BY started DESC LIMIT 1").fetchone()
            else:row=db.execute("SELECT * FROM training_sessions WHERE id=?",(session,)).fetchone()
            health=db.execute("SELECT payload,updated FROM training_health WHERE id=1").fetchone()
            recorder=json.loads(health["payload"]) if health else {}
            recorder["heartbeat_age_s"]=max(0.,time.time()-health["updated"]) if health else None
            recorder["alive"]=health is not None and time.time()-health["updated"]<=3.
            recorder["capture_age_s"]=time.monotonic()-recorder["capture_host_lower_s"] if "capture_host_lower_s" in recorder else None
            recorder["source_ages_s"]={k.removesuffix('_received_utc'):max(0.,time.time()-v) for k,v in recorder.items() if k.endswith('_received_utc')}
            recorder["learning_inputs_ready"]=recorder["alive"] and recorder["capture_age_s"] is not None and 0<=recorder["capture_age_s"]<=2. and all(recorder["source_ages_s"].get(k,float('inf'))<=2. for k in ('applied_control','ahrs_capture'))
            if not row:
                if session:raise ValueError("Unknown session_id")
                return dict(session=None,recorder=recorder,recording=False,executes_motion=False)
            counts=db.execute("SELECT e.kind,count(*) AS n FROM training_events t JOIN events e ON e.id=t.event_id WHERE t.session=? GROUP BY e.kind",(row["id"],)).fetchall()
            accepted=db.execute("SELECT COALESCE(sum(angular),0),COALESCE(sum(metric),0) FROM training_events WHERE session=?",(row["id"],)).fetchone()
            rejected={}
            for r in db.execute("SELECT rejections FROM training_events WHERE session=? AND rejections!='{}'",(row["id"],)):
                for key,value in json.loads(r[0]).items():rejected[key]=rejected.get(key,0)+value
            result=dict(session=dict(id=row["id"],label=row["label"],metadata=json.loads(row["metadata"]),
                started_utc=row["started"],stopped_utc=row["stopped"],
                status=("requested" if row["observed"] is None else ("recording" if recorder["alive"] else "recorder_unavailable")) if row["stopped"] is None else ("draining" if row["closed_observed"] is None else "stopped"),
                recorder_acknowledged=row["observed"] is not None,drained=row["closed_observed"] is not None,
                event_counts={r["kind"]:r["n"] for r in counts},angular_samples=accepted[0],metric_samples=accepted[1],rejected=rejected),
                recorder=recorder,recording=row["stopped"] is None and row["observed"] is not None and recorder["alive"],
                model_updates="Existing online learner accepts only qualified applied-control/sensor intervals; recording alone does not establish calibration",
                records_video=False,executes_motion=False)
            result["retention"]=dict(named_sessions="retained",new_unlabelled_telemetry="rolling_120_seconds",legacy_evidence="preserved")
            if action=="read":
                after=inputs.get("after_id",0);limit=inputs.get("limit",100)
                if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=500:
                    raise ValueError("after_id must be nonnegative and limit 1..500")
                rows=db.execute("SELECT e.*,t.angular,t.metric,t.rejections FROM training_events t JOIN events e ON e.id=t.event_id WHERE t.session=? AND e.id>? ORDER BY e.id LIMIT ?",(row["id"],after,limit+1)).fetchall()
                result["events"]=[dict(id=r["id"],kind=r["kind"],received_monotonic=r["received_monotonic"],recorded_utc=r["recorded_utc"],payload=json.loads(r["payload"]),training=dict(angular_samples=r["angular"],metric_samples=r["metric"],rejected=json.loads(r["rejections"]))) for r in rows[:limit]]
                result["next_after_id"]=rows[limit-1]["id"] if len(rows)>limit else None
                result["initial_model"]=json.loads(row["initial_model"])
                result["mutable_session"]=row["stopped"] is None or row["closed_observed"] is None
            return result
        finally:db.close()
