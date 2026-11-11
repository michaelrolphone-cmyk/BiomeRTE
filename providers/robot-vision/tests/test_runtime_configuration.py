import unittest
from biome_provider import VisionClient
from install_provider import make_config, launch_agents


class RuntimeConfigurationTests(unittest.TestCase):
    def test_queries_and_workers_have_no_automatic_runtime_cutoff(self):
        cfg = make_config("/tmp/repo", "/tmp/vision", "/tmp/gateway",
                          {"site": "test-site"}, {"node": "test-node"})
        self.assertIsNone(cfg["query_timeout_seconds"])
        self.assertIsNone(cfg["worker_timeout_seconds"])
        self.assertIsNone(VisionClient("/tmp/not-opened").timeout)
        self.assertGreater(cfg["health_timeout_seconds"], 0)

    def test_shutdown_allows_checkpoint_completion(self):
        cfg = make_config("/tmp/repo", "/tmp/vision", "/tmp/gateway",
                          {"site": "test-site"}, {"node": "test-node"})
        for spec in launch_agents(cfg, "/tmp/test-home").values():
            self.assertEqual(spec["ExitTimeOut"], 0)


if __name__ == "__main__":
    unittest.main()
