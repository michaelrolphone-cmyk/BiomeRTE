# Activation Field implementation status

Observed on the 24 GiB Apple Silicon Mac mini on November 5, 2026 UTC. The experimental Biome agent is installed and has generated through the live capability. This is not a claim that the entire scientific specification is complete or that useful memory has been demonstrated.

## Available now

Capability `agent.activation_field.delegate:1`, provider `079a0cdd8e67c087176fc939d3cabc767c76c60e13790b5ca75ca5593e50b27c`, resource `agent.activation-field.mac.079a0cdd8e67c087176f`. Native API and provider run as `org.biomerte.activation-field-api` and `org.biomerte.activation-field-agent` LaunchAgents. Idle service does not hold a model. Model operations use the existing shared model/image FIFO without an inference timeout.

Writes are disabled by default. Set `context.config.enabled=true` for a new test session. `baseline_pull=0` initially; it is adjustable. Existing sessions use versioned `action=config` calls. The normal Biome agent result path is not the Android private-chat encrypted transport.

## Observed engineering evidence

- `af-qualify-install-v5`: 20 mechanics/session tests and 13 complete-model checks passed. All 32 layers and 248 projections are covered. Disabled-field logits matched the reference exactly, frozen tensors stayed unchanged, zero-pull writes did not access the anchor, updated packed codes affected later logits, read-only preserved traces, complete checkpoint resume produced bit-exact next logits, repeated-seed replay reproduced weights, and exact change maps reconstructed weights with empty native caches.
- `af-install-v6-plugin`: 21 tests passed after adding versioned, idempotent cancellation. TypeScript compilation and local LM Studio installation succeeded. `af-launchagent-path-fix` reran all 21 tests successfully and corrected `sysctl` lookup under launchd's restricted PATH.
- `af-agent-live-enabled-2`: the actual Biome agent returned `Hello! How can I help you today` from the pinned full model, with writes enabled and baseline pull zero. It consumed 25 tokens, recorded 797,573 changed packed words across 11 field commits, and measured 12,049,625,678 bytes peak MLX allocation. This was an eight-output-token smoke test, not a memory experiment.
- `af-live-api-qualification`: ten live API checks passed: authentication, creation/config retry identity, changed-content retry rejection, stale-version rejection, session isolation, cancellation retry identity, cancellation boundary reporting, event-stream completion, and identical generation retry without replay. Cancellation occurred while queued at zero committed tokens; this does not claim an active-token cancellation test.
- `af-agent-disable-writes`: the live agent changed to disabled/read-only at committed boundary 25, version 2.
- `af-agent-resume-readonly`: the actual provider restored that session and generated another response. Native tokens advanced from 25 to 46, while field version stayed 11 and changed-word count stayed 797,573. The original prefix was not replayed and writes remained frozen.

Reproducible results and lockfiles are in `evidence/` and `lmstudio-generator/package-lock.json`. The model-manifest copy replaces only the machine-specific model path with a placeholder; set it to the pinned snapshot directory before use. The installed Mac manifest retains its verified real path.

Two-token full-model measurements: disabled 0.115965 seconds/token; enabled 1.658482 seconds/token; MLX peak 12,216,120,758 bytes. These are short engineering measurements, not sustained chat throughput. Enabled overhead exceeds the proposed 30% interactive target. Prefill, checkpoints and queue wait add latency. Baseline tensors occupy 5,038,041,600 bytes; writable packed tensors occupy 3,459,252,224 bytes.

Pinned model: `mlx-community/Qwen3.5-9B-4bit`, revision `8b2b98c00a6b4d291155e4890773ca8f769aee53`. Baseline identity: `5fe6cb50ccda48dc54ee1a3ae124fcd728f22966bd847572a261a87d629e63cc`.

## Remaining qualification and implementation

The installed LM Studio generator is `biome/activation-field`. External SDK invocation was tested with the existing Mac credential; LM Studio rejected it because that credential lacks `plugins.use` permission for this plugin. Authentication was not disabled or bypassed. Actual LM Studio-hosted streaming/session-switch/cancellation is therefore not yet qualified. `lmstudio-generator/qualify.mjs` exercises that real path once a suitably scoped token is provided. Token permissions are managed in LM Studio's Developer > Server Settings > Manage Tokens interface.

Full held-out memory, correction, 10,000-token stability, cross-domain retrieval and coherence experiments have not run. The initial CLI experiment harness is not a complete implementation of the scientific study: C3/C4 exact matched replay, paired multi-seed bootstrap analysis, expanded corpus templates/reverse/preferences probes, stability/executable-suite automation, and the calibrated coherence/retrieval evaluator remain incomplete. Optional persistent homology is not implemented. POST/GET experiment API routes remain unimplemented; experiments currently run through `fieldctl`.

Per-layer saturation/drift/update-norm reporting needs expansion beyond current candidate/changed counts and receipts. The exact change-map/import seam is callable and passed reconstruction tests; its provenance envelope still needs all formal section 20 fields before large-scale follow-up collection. Transform fitting and clustering remain explicitly deferred.

The LM Studio SDK currently resolves to dependencies for which npm reports a constrained-collection validation advisory in Zod 3. No incompatible major upgrade or SDK downgrade was applied.

Rhizome task `dd592c62-c65e-4026-ad3c-c5e75acecc27` retains remaining qualification. The external BPMN Intake projection did not materialize its Rhizome execution; that workflow was checkpointed without fabricating completion. The actual development execution and evidence are recorded separately.



## Completion continuation — source changes awaiting Mac qualification

The continuation adds durable POST/GET experiment jobs, explicit restart/resume with request identity and version checks, per-trial progress, atomic result publication, an exclusive run lock, frozen config/corpus/baseline identity, multi-seed execution, paired episode-bootstrap reports, reverse/preference probes, disjoint split templates, teacher-forced role segments, and a transcript-available reference. Existing legacy experiment directories are preserved; use a new output directory for this corpus version. `configs/experiment-confirmatory.json` pins the specified five seeds. This configuration is prepared, not executed.

Change-map exports now retain an encrypted source checkpoint and provenance. Materializing a map creates a separate saved branch without resetting/publishing into the active Engine. Invalid layout, duplicate/out-of-range groups, nonfinite deltas and checksum mismatches are rejected before activation. Checkpoint restore validates config/frequencies/counters before publishing and restores the saved configuration. These changes still require native full-model regression testing.

CPU validation: 14 continuation regressions and six existing session-control tests passed; all Python source compiles. No FastAPI route test or Metal regression was run in this continuation. The source changes are not deployed. The live installed agent's health action succeeded through Biome: ready, model unloaded, learning default false, baseline pull default zero. Normal Mac terminal operation `af-finish-inspect-mac-1` remains queued without a provider claim; its lease is `207c3628627912cf92296c2c913e9b2371733d6b398f309a77eea34cb6a7c153`. Read its original status before dispatching another command.

Remaining: exact C3/C4 replay, expanded per-layer quantization/drift metrics, the 10,000-token stability and executable-suite runner, calibrated cross-domain retrieval/coherence evaluator, optional homology, full scientific trials, native regression/deployment, and LM Studio permission/hosted verification. Full completion is not claimed. Run recovery must use the normal Biome terminal; the separate maintenance channel requires Michael's explicit confirmation.
