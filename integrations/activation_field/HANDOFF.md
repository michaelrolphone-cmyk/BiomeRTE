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

Full held-out memory, correction, 10,000-token stability, cross-domain retrieval and coherence experiments have not run. The continuation implements durable API/Biome jobs, paired multi-seed reporting, template splits and reverse/preferences probes, a checkpointed stability runner, and training-only calibrated descriptor evaluation. Exact C3/C4 matched replay remains explicitly unexecuted, as permitted by section 15.1; no noise-control comparison is claimed. Optional H0/H1 flag-complex summaries use Ripser 0.6.15 and default off. Native continuation qualification and deployment status is recorded below.

Per-layer write receipts now measure candidate/changed row events, changed/candidate/clipped codes, proposed and realized squared norms, and quantization squared error without allocating a floating-point model shadow. Stability reports include tiled relative row-drift percentiles and code saturation. Change-map provenance retains encrypted source checkpoints and input/run/config identities. Transform fitting and clustering remain explicitly deferred.

The LM Studio SDK currently resolves to dependencies for which npm reports a constrained-collection validation advisory in Zod 3. No incompatible major upgrade or SDK downgrade was applied.

Rhizome task `dd592c62-c65e-4026-ad3c-c5e75acecc27` retains remaining qualification. The external BPMN Intake projection did not materialize its Rhizome execution; that workflow was checkpointed without fabricating completion. The actual development execution and evidence are recorded separately.



## Recovery and continuation — November 5

The normal terminal recovered: original operation `af-finish-inspect-mac-1` succeeded. Commit `950fc3c96d540c7e2076ce421ac0c4d94d5e2428` was staged separately on the Mac and passed all 35 pytest tests and all 13 full-model assertions in operation `af-native-qualify-950fc3c`. Enabled cost was 1.6011964 seconds/token and disabled 0.1372970; this remains batch-only against the 30% interactive overhead target. The expanded continuation has now been deployed; the previous installed source is retained in source-before-cf30944.

The expanded continuation fixes pending batched writes being discarded on ordinary-cache reset, and preserves an explicit session configuration change made after a checkpoint when that checkpoint is later restored. Checkpoint metadata is validated before publication. Per-layer norm caches now publish alongside the actual weight transaction.

Experiments now expose kind=memory|stability|descriptors through POST/GET and Biome management actions. Stability checkpoints every 512 tokens, yielding model admission at each checkpoint; the 10,000-token target, held-out NLL, fixed executable/arithmetic/constraint suite, entropy, repetition and drift results remain separately recorded. Descriptor evaluation uses validator-labelled synthetic traces, disjoint templates/conversations, training-only standardization/classification, validation calibration, held-out AUROC/precision/recall/Brier/ECE, cross-domain retrieval controls, and separate structural/directed/temporal DTW channels. Its label remains trajectory novelty. Optional H0/H1 summaries use the full dissimilarity flag filtration, with no landmark substitution.

Resume freezes source hash as well as baseline/config/corpus identity. A changed implementation requires a new experiment output directory; old results are never mixed. Scientific success remains unestablished until actual studies finish. The exact C3/C4 controls are visibly unexecuted; no approximation is represented as matched replay.

LM Studio was retested during recovery and still denied the existing client permission `plugins.use` for `biome/activation-field`. Auth was not bypassed. Hosted streaming/session switching/cancellation remains blocked on that credential permission. The separately authenticated native API and Biome agent do not depend on it.

## Deployed continuation evidence

Installed source commit: `cf30944cb73771befb6cbbdc265ad1a47b803cc6`. The normal terminal path deployed it and restarted only its two LaunchAgents. Launchd initially reported its removal transition in progress; service absence was read back before normal bootstrap. No controller maintenance bypass was used.

- `af-reconcile-launchagent-restart`: 46 pytest tests passed on installed source. Eleven live authenticated API checks passed, including cancellation after a real streamed token and identical-request retry without replay.
- The same operation passed six live persisted-session assertions: enabled generation changed weights; a later explicit disabled/read-only toggle survived restoration; field version and changed-word count stayed unchanged while native token count advanced.
- `af-native-bfloat-drift-fix`: 11 native full-model assertions passed, including batched-tail commit, configuration restoration, per-layer measured norms/quantization error, invalid metadata rejection, and finite drift measurements across all 32 layers. The test exposed and fixed BF16-to-NumPy conversion in bounded drift tiles.
- `af-health-after-deploy-cf30944`: actual Biome provider returned ready, model unloaded, default learning false and baseline pull zero.
- `af-stability-engineering-smoke-cf30944`: started durable run `f68b23838bf14b4581bd3e24fa542097` through the live agent. It completed successfully and its status was read through the live Biome experiment_status action. It is an eight-token harness qualification with short probes, not the 10,000-token stability study. The report correctly records meets_duration=false and stability_target_met=false; baseline/post suite accuracy was 1/6 and NLL was 4.8242/4.8194, which is not evidence of useful memory.

The final installed-source full-model rerun (`af-final-full-model-cf30944`) passed all 13 assertions. With expanded measurement enabled it measured 2.41535 seconds/token versus 0.12423 disabled on this short two-token check. Interactive overhead remains above target; designated batch-only. Exact results are in `evidence/recovery-cf30944.json` and `evidence/live-final-cf30944.json`.

The capability is available for on-demand testing. Long scientific studies have executable configurations but are not silently started as ordinary background learning. Full scientific outcomes remain unestablished. LM Studio hosted qualification remains permission-blocked, so this record does not claim full specification acceptance.
