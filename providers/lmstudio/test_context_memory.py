import unittest
from context_memory import decision, memory_snapshot, estimate

G=1024**3
class MemoryPolicyTests(unittest.TestCase):
    def test_selects_64k_when_increment_and_headroom_fit(self):
        self.assertEqual(decision(16384,262144,{16384:7*G,32768:8*G,65536:10*G},
                                 {'available_bytes':6*G,'pressure_level':1})['target'],65536)
    def test_selects_32k_when_64k_would_consume_headroom(self):
        self.assertEqual(decision(16384,262144,{16384:7*G,32768:8*G,65536:10*G},
                                 {'available_bytes':4*G,'pressure_level':1})['target'],32768)
    def test_shrinks_already_loaded_64k_under_low_ram(self):
        self.assertEqual(decision(65536,262144,{32768:8*G,65536:10*G},
                                 {'available_bytes':G,'pressure_level':1})['target'],32768)
    def test_pressure_warning_chooses_32k_even_with_reclaimable_pages(self):
        self.assertEqual(decision(65536,262144,{32768:8*G,65536:10*G},
                                 {'available_bytes':9*G,'pressure_level':2})['target'],32768)
    def test_rechecks_memory_and_recovers_to_64k(self):
        estimates={32768:8*G,65536:10*G}
        low=decision(32768,262144,estimates,{'available_bytes':G,'pressure_level':1})
        high=decision(32768,262144,estimates,{'available_bytes':5*G,'pressure_level':1})
        self.assertEqual([low['target'],high['target']],[32768,65536])
    def test_insufficient_ram_for_32k_is_explicit(self):
        with self.assertRaisesRegex(MemoryError,'32K'):
            decision(16384,262144,{16384:7*G,32768:8*G,65536:10*G},
                     {'available_bytes':G//2,'pressure_level':1})
    def test_model_capacity_is_respected(self):
        self.assertEqual(decision(32768,32768,{32768:8*G},
                                 {'available_bytes':16*G,'pressure_level':1})['target'],32768)
    def test_available_memory_does_not_double_count_purgeable(self):
        vm='Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\nPages inactive: 200.\nPages speculative: 50.\nPages purgeable: 100.\n'
        def run(cmd,**kw):return vm if cmd==['vm_stat'] else '1\n'
        snap=memory_snapshot(run)
        self.assertEqual(snap['available_bytes'],350*16384)
    def test_estimation_does_not_load_model_and_preserves_parallelism(self):
        calls=[]
        def run(cmd,**kw):
            import subprocess
            self.assertEqual(kw['stderr'],subprocess.STDOUT)
            calls.append(cmd);return 'Estimated Total Memory: 10.04 GiB\n'
        self.assertEqual(estimate('qwen',65536,4,run),int(10.04*G))
        self.assertIn('--estimate-only',calls[0])
        self.assertEqual(calls[0][-2:],['--parallel','4'])
    def test_unknown_memory_and_estimate_are_not_assumed_safe(self):
        with self.assertRaises(RuntimeError):memory_snapshot(lambda *a,**k:'unknown')
        with self.assertRaises(RuntimeError):estimate('qwen',65536,4,lambda *a,**k:'unknown')
if __name__=='__main__':unittest.main()
