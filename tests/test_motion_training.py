import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from biomerte.motion_tracking import MotionRecorder
from biomerte.motion_training import TrainingStore,record,schema
from biomerte.motion_model import MotionModel


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/"observations.sqlite"
        self.db=sqlite3.connect(self.path)
        self.db.executescript("CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT,received_monotonic REAL,recorded_utc REAL,payload TEXT); CREATE TABLE model(id INTEGER PRIMARY KEY,payload TEXT,updated_utc REAL)")
        self.db.execute("INSERT INTO model VALUES(1,?,?)",(json.dumps(MotionModel().export()),time.time()));self.db.commit()
        schema(self.db);self.store=TrainingStore(self.path)

    def tearDown(self):self.db.close();self.tmp.cleanup()

    def test_idempotent_start_stop_conflict_and_page_boundaries(self):
        a=self.store.query('start',dict(session_id='manual-1',label='Room',metadata=dict(battery='fresh')))
        again=self.store.query('start',dict(session_id='manual-1',label='Room',metadata=dict(battery='fresh')))
        self.assertEqual(a['session']['started_utc'],again['session']['started_utc'])
        self.assertFalse(a['recording'])
        with self.assertRaises(ValueError):self.store.query('start',dict(session_id='other'))
        with self.assertRaises(ValueError):self.store.query('start',dict(session_id='manual-1',label='changed'))
        start=a['session']['started_utc']
        for i in range(1,4):
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?)",(i,'control',float(i),time.time(),json.dumps(dict(mask=5))))
            record(self.db,i,start+.0001,i==2,i==2,{'ambiguous':1} if i==1 else {})
        self.db.commit()
        one=self.store.query('read',dict(session_id='manual-1',limit=2))
        self.assertEqual([v['id'] for v in one['events']],[1,2])
        two=self.store.query('read',dict(session_id='manual-1',limit=2,after_id=one['next_after_id']))
        self.assertEqual([v['id'] for v in two['events']],[3])
        self.assertIsNone(two['next_after_id']);self.assertEqual(one['session']['angular_samples'],1)
        self.assertEqual(one['session']['rejected'],{'ambiguous':1})
        stopped=self.store.query('stop',dict(session_id='manual-1'))
        self.assertEqual(stopped['session']['status'],'draining')
        repeated=self.store.query('stop',dict(session_id='manual-1'))
        self.assertEqual(stopped['session']['stopped_utc'],repeated['session']['stopped_utc'])
        restarted=self.store.query('start',dict(session_id='manual-1',label='Room',metadata=dict(battery='fresh')))
        self.assertFalse(restarted['recording'])
        self.store.query('start',dict(session_id='manual-2'))

    def test_time_membership_excludes_before_start_and_after_stop(self):
        started=self.store.query('start',dict(session_id='bounds'))['session']['started_utc']
        stopped=self.store.query('stop',dict(session_id='bounds'))['session']['stopped_utc']
        for ident,at in enumerate((started-.1,(started+stopped)/2.,stopped+.1),1):
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?)",(ident,'vision',at,at,'{}'))
            record(self.db,ident,at,0,0,{})
        self.db.commit()
        result=self.store.query('read',dict(session_id='bounds'))
        self.assertEqual([e['id'] for e in result['events']],[2])
        self.assertTrue(result['session']['drained'])

    def test_dead_recorder_never_reports_recording(self):
        self.store.query('start',dict(session_id='dead'))
        self.db.execute("UPDATE training_sessions SET observed=?",(time.time()-20,))
        self.db.execute("INSERT INTO training_health VALUES(1,'{}',?)",(time.time()-20,));self.db.commit()
        result=self.store.query('status',{})
        self.assertFalse(result['recording']);self.assertFalse(result['recorder']['alive'])

    def test_recent_receipt_of_old_capture_does_not_qualify_learning(self):
        self.store.query('start',dict(session_id='stale'))
        now=time.time()
        health=dict(applied_control_received_utc=now,ahrs_capture_received_utc=now,capture_host_lower_s=time.monotonic()-100.)
        self.db.execute('INSERT INTO training_health VALUES(1,?,?)',(json.dumps(health),now));self.db.commit()
        result=self.store.query('status',dict(session_id='stale'))
        self.assertFalse(result['recorder']['learning_inputs_ready'])
        self.assertGreater(result['recorder']['capture_age_s'],99)

    def test_rolling_buffer_preserves_legacy_and_named_session_evidence(self):
        self.db.execute("INSERT INTO events VALUES(1,'vision',0,0,'{}')")
        self.db.execute("DELETE FROM training_retention");self.db.commit();schema(self.db)
        started=self.store.query('start',dict(session_id='retained'))['session']['started_utc']
        self.db.execute("INSERT INTO events VALUES(2,'sonar',0,0,'{}')")
        self.db.execute("INSERT INTO events VALUES(3,'control',0,0,'{}')")
        record(self.db,3,started+.0001,0,0,{});self.db.commit()
        recorder=MotionRecorder(self.path);recorder.offer('gap',dict(reason='prune'));recorder.close()
        self.assertIsNone(recorder.error)
        ids={r[0] for r in self.db.execute('SELECT id FROM events')}
        self.assertIn(1,ids);self.assertIn(3,ids);self.assertNotIn(2,ids)


class RecorderTests(unittest.TestCase):
    def test_null_ahrs_does_not_kill_recorder_and_session_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'observations.sqlite'
            recorder=MotionRecorder(path);recorder.offer('gap',dict(reason='test'));recorder.close()
            store=TrainingStore(path);store.query('start',dict(session_id='restart'))
            for iteration in range(2):
                recorder=MotionRecorder(path)
                recorder.offer('vision',dict(frame={},spatial_evidence=dict(ahrs=dict(attitude_reference=None))))
                recorder.offer('vision',dict(frame={},spatial_evidence=dict(ahrs=None)))
                recorder.offer('heartbeat',{})
                recorder.close()
                self.assertIsNone(recorder.error)
            status=store.query('status',dict(session_id='restart'))
            self.assertEqual(status['session']['event_counts']['vision'],4)
            self.assertEqual(status['session']['angular_samples'],0)
            self.assertGreater(status['session']['rejected']['missing_capture_clock_or_ahrs'],0)
            store.query('stop',dict(session_id='restart'))
            recorder=MotionRecorder(path);recorder.offer('heartbeat',{});recorder.close()
            self.assertEqual(store.query('status',dict(session_id='restart'))['session']['status'],'stopped')
