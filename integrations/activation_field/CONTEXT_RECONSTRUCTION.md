# Weight changes to context

This capability estimates a token stream from a committed Activation Field checkpoint and its immutable baseline. It never accepts a source conversation, source token list, source logits, or native caches as inference inputs. It reads only packed changed-weight groups and the recorded write configuration. A protected, independent snapshot survives source-session checkpoint cleanup.

The implementation derives first-layer input directions and firing-row signatures from signed weight differences, ranks the baseline vocabulary, then searches candidate sequences. Each candidate is replayed from fresh baseline weights and native state. The score measures squared residual over the union of target and candidate changes across every model projection. A low residual is not a probability that the text is the original history.

## Native Biome capabilities

All contracts take `arguments: obj` and return `result: obj`.

| Contract | Arguments |
|---|---|
| `model.context_reconstruction.start:1` | `target_checkpoint`, `expected_version: 0`, `purpose`, optional `options` |
| `model.context_reconstruction.status:1` | `run_id` |
| `model.context_reconstruction.cancel:1` | `run_id`, current `expected_version` |
| `model.context_reconstruction.export:1` | `run_id`, current `expected_version`, `allow_plaintext: true` |

For example, start with:

```json
{"target_checkpoint":"your-field-checkpoint","expected_version":0,"purpose":"portable context","options":{"max_tokens":8,"candidate_pool":16,"beam_width":2,"layout":"raw","role":"user"}}
```

Poll the returned run ID. Only `status: succeeded` means the job finished. The status response contains progress and residual measurements; explicit export returns the context, estimated token IDs, alternatives, provenance, and uncertainty. The original field service creates checkpoints through its existing `checkpoint` action.

Resume a cancelled, failed, or interrupted job by calling start with only `resume_run_id` and its current `expected_version`. The Biome operation ID is the request identity. A changed request must have a new ID. Resume reuses frozen inputs and completed replay scores; it requires the same implementation source. The process lock detects exited workers without a time cutoff.

Options are caller-visible search budgets, not hidden caps: max_tokens=8, candidate_pool=16, beam_width=2, sketch_rank=8, power_iterations=2, vocabulary_batch=512, seed=42, role=user, layout=raw. Layout may be message for one ChatML role block. Search cost grows with pool size, beam width and token depth; the entire vocabulary is scanned for proposals. No time or total export-size limit is imposed.

Inputs, source snapshots, replay scores and results are encrypted under local state protection. Explicit export reveals linguistic content through the authenticated capability. Context exports use a checksum-verified artifact tree even for small reports, preserving numeric formatting and signed-report integrity through JSON-normalizing controllers. They; recursively read its chunks/nodes via artifact.get. Source checkpoints and baseline files are never changed. Export does not reinject context, reinforce the model or record a human endorsement.

## Installation and qualification

Use the native field virtual environment:

```text
python -m activation_field.context_reconstruction.install
python -m pytest -q
python tests/qualify_context_service.py
```

The installer adds authenticated loopback API 8794 and a separate Biome worker. It leaves existing agent API 8791 and transform API 8793 running. All model activity enters the existing shared model/image queue. LM Studio is not required.

The native qualification creates one-token, multi-token and zero-change synthetic sources, runs durable jobs, verifies all 248 projections/32 layers, checks unchanged source digests and export integrity, and reports multi-token accuracy separately. Broader conversational reconstruction remains an empirical question: aggregate quantized updates discard ordering and may fit several histories. Mixed roles, configuration changes, resets and omitted template history may prevent close replay.

This is the inverse reconstruction experiment. Generic arbitrary-source summarization and the human-reviewed reinjection workflow described in Build Spec section 21 are separate capabilities; this extension does not claim to implement them.
