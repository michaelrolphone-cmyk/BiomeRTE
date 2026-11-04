# Biome Mac model admission

The private chat, generic/private image and LM Studio delegation adapters share
one metadata-only FIFO queue and one OS lock. Admission spans model preparation,
inference and image-cache cleanup. Model execution is serial on the Mac; Android
can submit and watch many independent requests concurrently.

- Image admission unloads every LM Studio instance before generation.
- LLM admission waits for ComfyUI to be idle, requests acknowledged cache release,
  unloads other LM Studio instances and verifies the requested model instance.
- The ComfyUI release endpoint runs the existing prompt-worker unload/reset path,
  waits for its reset generation to advance, and confirms zero resident models.
- Coding/delegated tasks explicitly request Qwen3.5 9B unless `context.model`
  specifies another model. Every tool-call inference reacquires admission.
- Idle preparation loads the configured Qwen default when no LLM is resident.
  An explicitly selected conversation model remains warm until other work needs
  another model; idle preparation does not replace it.
- Worker death releases the OS lock. Queue rows from dead process identities are
  removed. There is no job duration cutoff, lease-age inference, or prompt storage
  in the model queue. Capability operation durability remains in Biome.

`model_runtime.py` is installed at `~/.local/share/biome-model-runtime/`.
`config.json` holds `default_llm_model` and `default_load` settings, with the
existing coding context length retained. The idle helper is a launch agent named
`org.biomerte.model-runtime`. Queue inspection: `ModelRuntime().state()`.

The image runtime requires the additive `/biome/model-memory` loopback routes
from `memory_node.py`; they expose counts and reset generations only. Generic
and encrypted requests both pass through `ComfyBackend.generate`.

`deploy_model_runtime.py` uses current provider manifests to acquire fresh
maintenance reservations, verifies baseline source hashes, pauses only idle
providers, waits for existing work, and rolls back changed provider sources on
failure. Its sibling `baseline-sha256.json` must match the actual installation.
Stage the named provider files beside this script when installing from the
repository. Do not bypass a changed source guard; reconcile that source first.

Validation includes FIFO ordering, competing model unloads, custom-model warm
retention, default restoration, dead-owner recovery, isolated failures, request
privacy, exact delegated model selection, adapters holding admission during work,
and preservation of image instructions and explicit generation parameters.
