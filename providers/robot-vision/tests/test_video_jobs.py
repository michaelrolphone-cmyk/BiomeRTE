import base64
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from video_jobs import VideoJobs, locked
from video_worker import run_job, publish
from vision_config import atomic_json
from vision_store import VisionStore
from world_import import WorldImportQueue

CAL = dict(validated=True,calibration_id='test-camera',provenance='fixture',width=640,height=480,
           fx=500.,fy=500.,cx=320.,cy=240.,distortion=[0.,0.,0.,0.])

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name);atomic_json(self.state/'config.json',{})
        self.jobs=VideoJobs(self.state,launch=False)
        self.i=str(uuid.uuid4())
    def create(self,calibration=CAL):
        return self.jobs.create(dict(request_id=self.i,name='room.mp4',size=6,calibration=calibration))
    def upload(self,calibration=CAL):
        self.create(calibration)
        self.jobs.chunk(dict(id=self.i,offset=0,data=base64.b64encode(b'abcdef').decode()))
        return self.jobs.finish(self.i)
    def test_upload_idempotence_and_conflicting_retries(self):
        self.create();body=dict(id=self.i,offset=0,data=base64.b64encode(b'abc').decode())
        self.assertEqual(self.jobs.chunk(body)['uploaded_bytes'],3)
        self.assertEqual(self.jobs.chunk(body)['uploaded_bytes'],3)
        with self.assertRaisesRegex(ValueError,'Conflicting'):self.jobs.chunk({**body,'data':'eHl6'})
        with self.assertRaises(ValueError):self.jobs.chunk({**body,'offset':4})
        with self.assertRaisesRegex(ValueError,'incomplete'):self.jobs.finish(self.i)
        self.assertEqual(self.create()['uploaded_bytes'],3)
    def test_upload_accepts_uncalibrated_but_reconstruction_requires_calibration(self):
        self.assertEqual(self.upload(None)['state'],'awaiting_calibration')
        with self.assertRaisesRegex(ValueError,'calibration'):self.jobs.start(dict(id=self.i))
        self.assertEqual(self.jobs.start(dict(id=self.i,calibration=CAL))['state'],'queued')
    def test_cancel_before_publication_and_reject_path_traversal(self):
        self.upload();self.assertEqual(self.jobs.cancel(self.i)['state'],'cancelled')
        with self.assertRaises(ValueError):self.jobs.folder('../runtime.json')
        with self.jobs.edit(self.i) as value:value['state']='integrating'
        with self.assertRaisesRegex(ValueError,'retained'):self.jobs.cancel(self.i)
    def test_live_worker_lock_prevents_inferred_interruption(self):
        self.upload()
        with self.jobs.edit(self.i) as value:value.update(state='reconstructing',launched_at=0)
        with locked(self.jobs.folder(self.i)/'run.lock'):
            self.assertEqual(self.jobs.get(self.i)['state'],'reconstructing')
        self.assertEqual(self.jobs.get(self.i)['state'],'interrupted')
    def test_failed_reconstruction_never_creates_shared_map(self):
        self.upload();self.jobs.start(dict(id=self.i))
        class Backend:
            def __init__(self,*a):pass
            def start(self,*a):raise RuntimeError('native initialization failed')
            def close(self):pass
        run_job(self.jobs,self.i,backend_factory=Backend)
        self.assertEqual(self.jobs.get(self.i)['state'],'failed')
        self.assertFalse((self.state/'observations').exists())
    def test_publication_uses_one_checkpoint_and_queue_id_on_retry(self):
        self.upload();folder=self.jobs.folder(self.i);native=folder/'native';native.mkdir()
        value=self.jobs.get(self.i)
        atomic_json(native/'backend_manifest.json',dict(map_id=value['map_id'],source_frames={}))
        atomic_json(native/'map.json',{'fixture':True})
        (native/'loop_closing.db').write_bytes(b'fixture')
        with self.jobs.edit(self.i) as current:current.update(source_sha256='abc',native_saved=True,state='publishing')
        entities=lambda *args:{('keyframe','k1'):{'position':[0,0,0]},('sparse_point','p1'):{'xyz':[1,2,3]}}
        publish(self.jobs,self.i,entities_reader=entities)
        job=self.jobs.get(self.i);queue=WorldImportQueue(self.state)
        self.assertEqual(job['state'],'integrating')
        first=queue.get(self.i)
        self.assertEqual(first['map_id'],job['map_id'])
        self.assertFalse(native.exists());self.assertFalse((folder/'video.bin').exists())
        # Retry resumes handoff from persisted identities without decoding,
        # recreating a map, or trying to read deleted uploaded media.
        run_job(self.jobs,self.i)
        self.assertEqual(queue.get(self.i)['checkpoint_id'],first['checkpoint_id'])
        store=VisionStore(self.state/'observations')
        self.assertEqual(len(store.list_maps()),1);store.close()

if __name__=='__main__':unittest.main()
