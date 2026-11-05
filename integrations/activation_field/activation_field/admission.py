"""Reuse the deployed FIFO/OS-lock admission without changing other adapters."""
import sys
from pathlib import Path
from contextlib import contextmanager


@contextmanager
def model_slot(progress=None):
    root=Path.home()/'.local/share/biome-model-runtime'
    sys.path.insert(0,str(root))
    from model_runtime import ModelRuntime,LocalModels
    class FieldModels(LocalModels):
        def prepare(self,kind,model,progress=None):
            self.wait_image_idle(progress);self.unload_images();self.unload_llms()
            return model
        def release(self,kind):pass
    # The caller MUST destroy all Engine arrays before exiting this context.
    with ModelRuntime(models=FieldModels()).slot('llm','activation-field-qwen3.5-9b',progress):yield
