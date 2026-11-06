# Weight changes to context

This capability returns approximate token hypotheses from a committed Activation Field checkpoint and its immutable baseline. It never accepts a source conversation, source token list, source logits, or native caches as inference inputs. It reads only packed changed-weight groups and the recorded write configuration. A protected, independent snapshot survives source-session checkpoint cleanup.

The default `mode=first_pass` derives first-layer input directions and firing-row signatures from signed weight differences, ranks the baseline vocabulary once, and returns those approximate tokens immediately. It does not replay candidate sequences, require exact reconstruction, or filter the result through a relevance or residual gate. `candidate_pool` controls the number of returned proposals. The raw text concatenates proposals in descending proposal-score order, explicitly **not recovered source order**; repetitions and chronology are not inferred.

The previous sequence search remains available only through explicit `mode=replay`. Each search candidate is replayed from fresh baseline weights and native state. Its score measures squared residual over the union of target and candidate changes across every model projection. A low residual is not a probability that the text is the original history.

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
{"target_checkpoint":"your-field-checkpoint","expected_version":0,"purpose":"portable context","options":{"mode":"first_pass","candidate_pool":16}}
```

Poll the returned run ID. Only `status: succeeded` means the job finished. The status response contains progress and residual measurements; explicit export returns the context, estimated token IDs, alternatives, provenance, and uncertainty. The original field service creates checkpoints through its existing `checkpoint` action.

Resume a cancelled, failed, or interrupted job by calling start with only `resume_run_id` and its current `expected_version`. The Biome operation ID is the request identity. A changed request must have a new ID. Resume reuses frozen inputs and completed replay scores; it requires the same implementation source. The process lock detects exited workers without a time cutoff.

Options are caller-visible budgets, not hidden caps: mode=first_pass, max_tokens=8, candidate_pool=16, beam_width=2, sketch_rank=8, power_iterations=2, vocabulary_batch=512, seed=42, role=user, layout=raw. Layout may be message for one ChatML role block. The entire vocabulary is scanned once for proposals. max_tokens, beam_width, role and layout affect only optional replay; its search cost grows with pool size, beam width and token depth. No time or total export-size limit is imposed.

Inputs, source snapshots, replay scores and results are encrypted under local state protection. Explicit export reveals linguistic content through the authenticated capability. Context exports use a checksum-verified artifact tree even for small reports, preserving numeric formatting and signed-report integrity through JSON-normalizing controllers. Recursively read the tree's chunks/nodes via artifact.get. Source checkpoints and baseline files are never changed. Export does not reinject context, reinforce the model or record a human endorsement.

## Installation and qualification

Use the native field virtual environment:

```text
python -m activation_field.context_reconstruction.install
python -m pytest -q
python tests/qualify_context_service.py
```

The installer adds authenticated loopback API 8794 and a separate Biome worker. It leaves existing agent API 8791 and transform API 8793 running. All model activity enters the existing shared model/image queue. LM Studio is not required.

The legacy native replay qualification explicitly selects mode=replay and creates one-token, multi-token and zero-change synthetic sources, runs durable jobs, verifies all 248 projections/32 layers, checks unchanged source digests and export integrity, and reports multi-token accuracy separately. Broader conversational reconstruction remains an empirical question: aggregate quantized updates discard ordering and may fit several histories. Mixed roles, configuration changes, resets and omitted template history may prevent close replay.

This is the inverse reconstruction experiment. Generic arbitrary-source summarization and the human-reviewed reinjection workflow described in Build Spec section 21 are separate capabilities; this extension does not claim to implement them.

## Measured native results (2026-11-05)

On the full Qwen3.5-9B baseline, the one-token synthetic checkpoint reconstructed ` jade` (token 82082) with zero replay residual across all 248 projections and 32 layers. The two-token checkpoint ` jade cedar` reconstructed only ` jade`, with relative squared error 0.5358201129950034 under the declared four-candidate, rank-two search. The zero-change checkpoint produced no tokens and reported insufficient signal. These measurements do not establish general conversational recovery.

The first end-to-end harness stopped at export integrity verification: inline JSON transport did not preserve the report's original representation. The repair sends every signed context report as immutable artifact bytes. Native deployment and repeat export verification remain pending; the capability is not yet fully qualified. The source checkpoint and completed inference runs can be reused for this transport-only check. See `qualification/context-reconstruction-20261105.json` for exact run IDs and continuation details.


## First-pass corpus trial

Run `qualification/run_subject_corpus_01.py qualification/subject-corpus-01.json` through the native Biome terminal. It reuses the original 415-word orchard corpus ingestion and checkpoint, selects first_pass explicitly, and retains a separate `subject-corpus-01-first-pass.json` evidence file. It checks zero replay evaluations, export integrity and source preservation, then records the returned text unchanged. Topic relevance is reviewed from that raw output, never used to block or rewrite it.
