import unittest
from worker import watch
class Child:
    def __init__(self, result=None): self.returncode=result
    def poll(self): return self.returncode
class WatchTests(unittest.TestCase):
    def test_stop(self): self.assertEqual(watch(Child(),lambda:True,lambda:True,10),'stopped')
    def test_parent_loss(self): self.assertEqual(watch(Child(),lambda:False,lambda:False,10),'interrupted')
    def test_deadline(self): self.assertEqual(watch(Child(),lambda:True,lambda:False,0),'completed')
    def test_backend_failure(self): self.assertEqual(watch(Child(3),lambda:True,lambda:False,10),'failed')
if __name__=='__main__': unittest.main()
