# Activation Field — deployment and qualification

Updated November 5, 2026 UTC. The native experimental agent is installed on the 24 GiB Mac mini and available through Biome. Learning is off by default; baseline pull is zero. Useful learned memory and full scientific acceptance are not established.

## Current deployment

Installed code: `06c37c84750ba77d98da4f20d460dbd0a98aca9b`.
Capability: `agent.activation_field.delegate:1`.
Resource: `agent.activation-field.mac.079a0cdd8e67c087176f`.
Provider: `079a0cdd8e67c087176fc939d3cabc767c76c60e13790b5ca75ca5593e50b27c`.

Root: `~/.local/share/biome-activation-field`. Native API: authenticated loopback `127.0.0.1:8791`. LaunchAgents: `org.biomerte.activation-field-api` and `org.biomerte.activation-field-agent`. Previous source is retained at `source-before-06c37c8`. Idle service holds no model. All model work uses the existing shared model/image FIFO; no inference timeout was added.

The normal terminal operation `af-finish-deploy-06c37c8` completed with exit code 0. It waited for the descriptor run and idle sessions, replaced only this package, restarted its two services, and passed live session/report checks. The shared LM Studio queue cleared without intervention. A proposed worker restart stopped at its precondition before sending a signal; no LM Studio or controller service was changed.

Pinned model: `mlx-community/Qwen3.5-9B-4bit`, revision `8b2b98c00a6b4d291155e4890773ca8f769aee53`; baseline hash `5fe6cb50ccda48dc54ee1a3ae124fcd728f22966bd847572a261a87d629e63cc`.

## What works

All 32 layers and 248 eligible projections participate in direct signed packed-weight updates. The baseline, scales, offsets, embeddings and other frozen tensors stay unchanged. Native hybrid caches, weights, activity traces, configuration, sampling counters and opaque input identity can be checkpointed and restored. Change maps preserve exact replacement codes, input/source provenance and an encrypted source checkpoint; materialization does not mutate the active field.

Versioned session settings remain authoritative after checkpoint restoration. Ordinary cache reset commits a pending batch. An entire-session reset clears sampling/token counters and input identity. Per-layer diagnostics measure actual quantized changes, norm drift and quantization error without a full floating-point model shadow.

Durable memory, stability and descriptor experiment jobs expose start/resume/status through the API and Biome. Source, baseline, corpus and configuration are pinned. Changed-source resume is rejected. Completed reports are returned by experiment status; restarting does not silently restart interrupted work. Optional H0/H1 summaries use Ripser 0.6.15 and default off.

## Measured evidence

| Check | Observed result |
|---|---|
| Automated suite on final code | 47 passed |
| Native full-model provenance and mechanics | 16 assertions passed |
| Native continuation boundaries | 11 assertions passed |
| Authenticated live API | 11 passed, including active-token cancellation |
| Persisted-session regression after final deployment | 6 passed |
| Durable stability engineering smoke | Completed 8 tokens; explicitly below 10,000-token criterion |
| Full-model descriptor engineering run | Completed 54 traces: 18 train, 18 validation, 18 test |

The 16 native assertions establish reference-equivalent disabled inference, actual packed-weight changes that affect later logits, frozen tensors, zero-pull behavior, bit-exact checkpoint/replay/change-map round trips, empty caches on import, reset counters and input/source provenance. They ran on `f6841ea`; final `06c37c8` adds report presentation only and passed the 47-test suite. Live session checks then passed on the installed final code.

Latest short native measurement: enabled 2.577 seconds/token; disabled 0.116 seconds/token. This two-token check exceeds the proposed 30% interactive overhead target. It is a batch experiment implementation, not an interactive-performance pass.

Evidence files:
- `evidence/provenance-f6841ea.json`: all 16 native assertions and final 47-test result.
- `evidence/deployment-06c37c8.json`: final deployment, live checks and measured experiment summaries.
- `evidence/recovery-cf30944.json`: 11 native boundary checks, active cancellation, session configuration and exact dependencies.
- `evidence/live-final-cf30944.json`: earlier full-model rerun and stability smoke.

## Experiment results

Descriptor run: `084c6bca02934817a44629bbf5983600`, completed before the source update, pinned source hash `22fe4fc8662d9c83b2404b504b3b656c380c9ae0cb34d0fabe2be99c354977e0`. Learning was disabled. Graph-descriptor held-out correctness AUROC was **0.5556**, below the **0.75** target. Cross-domain pattern retrieval was **0.8889**, versus activation-mean **0.7778**, length **0.3333**, and shuffled descriptors **0.3889**. These are descriptive results on 18 synthetic held-out traces; they do not establish general reasoning or hallucination detection. The label remains `trajectory_novelty_unvalidated`. Persistent homology was off in this run.

Stability run: `f68b23838bf14b4581bd3e24fa542097`. The eight-token engineering smoke completed and resumed status across deployment. NLL relative change was -0.00100346 and suite accuracy change was 0. The report correctly sets `meets_duration=false` and `stability_target_met=false`. This is not evidence of useful memory or sustained stability.

Full traces, classifier data and reports remain in the Mac root's `experiments/<run_id>` directories. Both completed jobs can be read through `context.action=experiment_status` with their run ID.

## Use on demand

Acquire the advertised Biome resource and invoke with a unique operation ID:

```json
{"request":"Reply with one word: ready.","context":{"config":{"enabled":true,"baseline_pull":0},"max_tokens":8}}
```

The response supplies the session ID and version; preserve them for later calls. Set `persist:true` explicitly to retain a session across restarts. For an existing session, use the versioned `config` action to change learning. The default is off. Native Biome results use the ordinary result path, not the Android private-chat encrypted transport. See README for transcript, reset, checkpoint, import, cancellation and experiment controls.

## Remaining qualification

The local LM Studio generator `biome/activation-field` is installed and compiled. Its existing external SDK credential was retested and lacks `plugins.use` permission for this generator. LM Studio-hosted streaming, switching and cancellation are therefore unqualified. Authentication was not bypassed. With an appropriately scoped credential, run `lmstudio-generator/qualify.mjs`. The native Biome capability works independently of this permission.

Full multi-seed held-out memory/correction and 10,000-token stability studies remain unrun. Exact C3/C4 matched controls are explicitly unexecuted as permitted by specification section 15.1; no approximate replay is presented as a matched control. The small descriptor run missed the correctness target. Generalized transform fitting and clustering remain a deferred follow-up, supported by the change-map interface.

No long scientific study or background learning remains running from this recovery. No scientific-success claim or full-specification acceptance is made.

## Durable task record

[Rhizome task](https://tasker-workboard.cantikash.chatgpt.site/?task=dd592c62-c65e-4026-ad3c-c5e75acecc27) · [Source PR](https://github.com/michaelrolphone-cmyk/BiomeRTE/pull/27)

Execution: `e8f40168-2ace-449c-8742-281b0bbebb05`. Humus context: `activation-field-recovery-20261105`. The external BPMN Intake projection remained parked; actual development and evidence were recorded without fabricating workflow completion.
