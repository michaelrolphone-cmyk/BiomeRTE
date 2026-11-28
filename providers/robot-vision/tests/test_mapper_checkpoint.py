import queue
import threading
import unittest
from mapper_checkpoint import install_owner_capture, capture_on_mapper

class Mapper:
    def __init__(self):
        self.queue = queue.Queue()
        self.queue_condition = threading.Condition()
        self.stop_mutex = threading.Lock()
        self.is_running = True
        self.stop_requested = self.stopped = False
        self.seen = []
    def pop_keyframe(self):
        return self.queue.get()
    def run(self):
        while self.is_running:
            value = self.pop_keyframe()
            if value is not None:
                if value[0] == "end": return
                self.seen.append(value[0])
    def close(self):
        self.is_running = False
        self.queue.put(("end",None,None,None))

class OwnerCaptureTests(unittest.TestCase):
    def make_mapper(self):
        class TestMapper(Mapper): pass
        install_owner_capture(TestMapper)
        return TestMapper()

    def test_fifo_capture_runs_on_owner_and_keeps_prior_keyframes(self):
        mapper=self.make_mapper()
        mapper.queue.put(("kf1",None,None,None));mapper.queue.put(("kf2",None,None,None))
        thread=threading.Thread(target=mapper.run,daemon=True);thread.start()
        try:
            seen, owner = capture_on_mapper(mapper,lambda:(list(mapper.seen),threading.get_ident()))
            self.assertEqual(seen,["kf1","kf2"])
            self.assertEqual(owner,thread.ident)
        finally:mapper.close();thread.join(2)

    def test_culling_completes_before_capture_takes_map_lock(self):
        mapper=self.make_mapper()
        map_lock, keyframe_lock = threading.RLock(),threading.RLock()
        culling, finish, done = threading.Event(),threading.Event(),threading.Event()
        output=[]
        def run():
            with keyframe_lock:
                culling.set()
                self.assertTrue(finish.wait(2))
                with map_lock: pass
            mapper.run()
        owner=threading.Thread(target=run,daemon=True);owner.start();self.assertTrue(culling.wait(2))
        def snapshot():
            with map_lock, keyframe_lock:return "immutable"
        def caller():
            output.append(capture_on_mapper(mapper,snapshot));done.set()
        caller_thread=threading.Thread(target=caller,daemon=True);caller_thread.start()
        try:
            self.assertTrue(map_lock.acquire(blocking=False));map_lock.release()
            self.assertFalse(done.is_set())
            finish.set();self.assertTrue(done.wait(2))
            self.assertEqual(output,["immutable"])
        finally:finish.set();mapper.close();owner.join(2);caller_thread.join(2)

    def test_callback_failure_does_not_kill_mapper(self):
        mapper=self.make_mapper();t=threading.Thread(target=mapper.run,daemon=True);t.start()
        try:
            def fail():raise ValueError("capture failed")
            with self.assertRaisesRegex(ValueError,"capture failed"):capture_on_mapper(mapper,fail)
            self.assertEqual(capture_on_mapper(mapper,lambda:42),42)
            self.assertTrue(t.is_alive())
        finally:mapper.close();t.join(2)

    def test_stopped_mapper_rejects_without_orphaning_a_call(self):
        mapper=self.make_mapper();mapper.is_running=False
        with self.assertRaisesRegex(RuntimeError,"stopped"):capture_on_mapper(mapper,lambda:None)
        self.assertTrue(mapper.queue.empty())
