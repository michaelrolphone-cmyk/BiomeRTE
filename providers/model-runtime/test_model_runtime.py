import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_runtime import LocalModels


class DefaultModelIdleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.models = LocalModels(self.root)
        self.config = {
            "default_llm_model": "qwen3.5-9b",
            "default_load": {"context_length": 16384, "parallel": 4},
            "default_model_idle_seconds": 120,
        }
        self.catalog = {"type": "llm", "key": "qwen3.5-9b", "loaded_instances": []}

    def write_config(self):
        (self.root / "config.json").write_text(json.dumps(self.config))

    def test_idle_worker_leaves_models_unloaded(self):
        self.write_config()
        with patch.object(self.models, "comfy") as comfy, patch.object(self.models, "models") as catalog:
            self.assertFalse(self.models.warm_default())
            comfy.assert_not_called()
            catalog.assert_not_called()

    def test_default_load_sets_idle_expiry_and_preserves_context_and_parallelism(self):
        self.write_config()

        def load_model(argv, **kwargs):
            self.assertEqual(argv[1:], [
                "load", "--yes", "--ttl", "120", "--identifier", "qwen3.5-9b",
                "--context-length", "16384", "--parallel", "4", "--", "qwen3.5-9b",
            ])
            self.catalog["loaded_instances"] = [{"id": "qwen3.5-9b"}]

        with patch.object(self.models, "models", return_value=[self.catalog]), patch(
            "model_runtime.subprocess.run", side_effect=load_model
        ) as run:
            self.assertEqual(self.models.select("qwen3.5-9b"), "qwen3.5-9b")
            run.assert_called_once()

    def test_existing_instance_is_reused_without_changing_its_lifetime(self):
        self.write_config()
        self.catalog["loaded_instances"] = [{"id": "operator-selected"}]
        with patch.object(self.models, "models", return_value=[self.catalog]), patch(
            "model_runtime.subprocess.run"
        ) as run:
            self.assertEqual(self.models.select("operator-selected"), "operator-selected")
            run.assert_not_called()

    def test_unconfigured_policy_preserves_api_load(self):
        self.config.pop("default_model_idle_seconds")
        self.write_config()

        def load_model(path, body):
            self.assertEqual(path, "/api/v1/models/load")
            self.assertEqual(body, {"model": "qwen3.5-9b", "context_length": 16384, "parallel": 4})
            self.catalog["loaded_instances"] = [{"id": "qwen3.5-9b"}]
            return {"instance_id": "qwen3.5-9b"}

        with patch.object(self.models, "models", return_value=[self.catalog]), patch.object(
            self.models, "lm", side_effect=load_model
        ) as api:
            self.assertEqual(self.models.select("qwen3.5-9b"), "qwen3.5-9b")
            api.assert_called_once()
