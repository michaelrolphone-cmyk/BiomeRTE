# Activation Field native transform qualification

The transform implementation and nine Biome capabilities are deployed on the Mac. This package contains an actual exported function and measured native-model results. It is an engineering trial of a **partial fit**, not a demonstrated portable memory or successful model compression.

Source: [BiomeRTE PR 37](https://github.com/michaelrolphone-cmyk/BiomeRTE/pull/37), based on the native Activation Field implementation in PR 27. The implementation requires no LM Studio interface. See `source-references.json` for exact model, implementation, job and artifact identities, and `dependencies.json` for the installed runtime versions.

## Candidate and source

The complete `Activation-Field-Partial-Candidate.afwt` is 149,092 bytes: 148,342 manifest bytes, 528 region-parameter bytes and 222 container bytes. The candidate is distributed in the downloadable qualification package and can also be obtained through the export capability documented below. Its SHA-256 is `0e23ded994da97f09bc2fb60ff588a0527425bfe06dd225ff52c159ed900b7f2`.

It maps all 248 eligible projections of `mlx-community/Qwen3.5-9B-4bit`, revision `8b2b98c00a6b4d291155e4890773ca8f769aee53`. The baseline identity is `5fe6cb50ccda48dc54ee1a3ae124fcd728f22966bd847572a261a87d629e63cc`. Frozen model tensors remain unchanged.

The 11 fitted regions all belong to `language_model.model.layers.0.linear_attn.in_proj_b`. Every other mapped tensor receives zero transform delta. The protected target is the immutable `afwt-qualification-source` checkpoint, hash `080983ab1cb8276e03435e65194a546799e04b82ed5906aec883850f2147057c`, derived from an eight-token native engineering experiment. The full fitting run remains incomplete. The saved candidate was selected before its final numerical diagnostics; later fitting is exploratory following those disclosed results.

The artifact contains exactly `manifest.json` and `regions.bin`. It contains no hidden sampled weights or residual maps. The 1 MB goal is advisory; there is no default time, region-count or total artifact-size cap. Gateway transfers use checksum-addressed chunks and hierarchical manifests without limiting the complete artifact.

## Native evidence

The native test suite passed 75 tests in 1.92 seconds, including format, geometry, quantization, recovery, typed errors, large artifact transport, independent read-only trials and distinct floating/quantized metrics. The exact test receipt is in `test-receipts.json`.

`portability-numerical.json` records the isolated restoration and full numerical scan. A fresh macOS sandbox process was denied reads of both the evolved checkpoint and the fitting observations. It loaded the baseline and exported artifact into an independent state directory. After that child exited, all 250 plaintext tensor records and their chunk hashes matched the ordinary materialization. Different encrypted checkpoint manifest hashes are expected because their creation metadata differs; tensor content was compared directly.

The numerical scan covered all 248 eligible tensors and 6,918,504,448 logical coordinates. The target changed 576,158 codes. This candidate reproduces only 5 of those codes, adds 33,527 changes where the target equaled baseline, and has relative quantized delta error 1.0073434478513361. A zero transform has relative delta error 1, so this partial candidate is slightly worse than leaving the baseline unchanged by that measure. Small global RMSE is dominated by the billions of unchanged coordinates and must not be interpreted as successful recovery.

`rmse` and `mae` measure the fitted real-valued delta before rounding. `quantized_rmse`, `quantized_mae` and their changed/unchanged variants measure the restored quantized weights against the evolved target. Relative delta error also uses the restored quantized weights. Full per-tensor values and untouched diagnostic partitions are in the JSON report; sampled validation losses are separately recorded in `candidates.json`.

| Full-scan measure | Before rounding | Restored quantized weights |
| --- | ---: | ---: |
| Global RMSE | 4.82530461e-05 | 4.82860506e-05 |
| Global MAE | 4.09555386e-07 | 4.0508185e-07 |
| Changed-coordinate RMSE | 0.0052526485 | 0.00525264845 |
| Changed-coordinate MAE | 0.00472168897 | 0.00472168547 |
| Unchanged-coordinate RMSE | 5.54053192e-06 | 5.82099854e-06 |
| Unchanged-coordinate MAE | 1.63447619e-08 | 1.18711444e-08 |

The incomplete fit had recorded 3,400.47 seconds of fitting time and 1,239,253,289 bytes of job disk at its saved 100-region checkpoint. This includes protected temporary observations and retained candidates; the exported candidate remains only 149,092 bytes. `fit-cost-snapshot.json` records the exact observation. There is no completed-fit duration yet.

The isolated child took 204.34 seconds including model loading; its materialization took 197.47 seconds. The ordinary materialization took 209.83 seconds, recorded a 5,798,821,888-byte process high-water RSS and used 159,168 bytes in its job directory. RSS is a process lifetime high-water measure, not incremental memory; job disk excludes the separately stored checkpoint and shared baseline. The first complete numerical scan took 428.67 seconds. The supplemental scan added explicit quantized RMSE/MAE fields and took 460.22 seconds. Its original floating errors, quantized squared error and code counts exactly match the first scan. The driver then resumed the saved fit through Biome at version 16; see `fit-continuation.json`.

`comparison.json` contains actual sequential Baseline / Evolved / Restored evaluation with identical teacher-forced prefixes and separate free generation. Every probe starts with empty native state, writes disabled and decay disabled.

| General probe | Baseline score | Evolved score | Restored score | KL(target → baseline) | KL(target → restored) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Arithmetic | 1 | 1 | 1 | 0.00150209 | 0.000948515 |
| Code | 1 | 1 | 1 | 0.000937495 | 0.000938797 |
| Instruction | 1 | 1 | 1 | 0.00138792 | 0.00124450 |

Free outputs were respectively `5`, `35` and `ready` for all three states. The retention ratio is undefined because baseline and evolved scores are equal. Paired score-bootstrap intervals are all [0, 0] for these three smoke probes. They do not establish a learned memory, correction or preference effect, nor absence of wider degradation. Comparison took 26.98 seconds, recorded a 5,072,240,640-byte process high-water RSS and used 40,730,033 bytes in its job directory. No assessment by Michael is claimed.

`qualification.png` separates the saved sampled fitting curve from matched-prefix behavioral measurements. It does not extrapolate to a completed fit or a successful 1 MB context payload.

## Use through Biome

All nine contracts are version `:1` under `model.field_transform`: `fit`, `inspect`, `export`, `materialize`, `compare`, `status`, `cancel`, `trial` and `assessment`. Their resource is `model.field-transform.mac.95b20447b68a57272f47`. Inputs have the shape `{"arguments": {...}}`; each normal Biome submission needs a unique operation ID. Read its `cap.status` result, then the returned durable run's status. An accepted job is not a completed job.

Inspect this candidate with:

```json
{"arguments":{"run_id":"b3b31360dc2748f18fa55568dba076eb","candidate_id":"candidate-a940e6537235c7abbdd5"}}
```

Read the same fit's current progress through `model.field_transform.status:1`, using `run_id` above. The tested restored checkpoint is `transform-86a6b3e8e7ef41218be0e1ffa1b7213c`; its ordinary manifest hash is `1373c3cb4564b01de5823510d8fb4ff4312122eed9964fc597612f021035e2d9`.

The completed comparison run is `8ab1a530c6144f64a6874b7ba8c7d1a8`. `trial` can create fresh independent Baseline / Evolved / Restored sessions, optionally blind, using that run ID and its **current** version from `status`. It returns session IDs and versions. Send a prompt through `agent.activation_field.delegate:1` with the chosen `session_id` and `expected_version`; use its returned version for the next turn. Selecting the independent Baseline session returns to the original model. These trial sessions are read-only. Their creation does not switch another conversation's model or enable learning.

The original three independent trial sessions were Baseline `ce37d4d9-a33b-417b-a576-6d8fb5937129`, Evolved `39623020-3cd7-4fbf-ba74-4545d9423433`, Restored `6d21c43f-82ff-406e-b7ea-7e4fe6af0d14`; all were version 0 when created. Check current versions before reuse. `assessment` records explicit attribution and candidate choice, without manufacturing a human endorsement.

## Local restoration and reproduction

Check out the implementation revision in `source-references.json` and use the native environment recorded in `dependencies.json`. `fit-config.json` is the exact fit configuration. The reusable evaluator, artifact schema/validation and CLI are under `integrations/activation_field/activation_field/transforms` in the linked source. `TRANSFORMS.md` documents the full API and format.

With the matching original model manifest, inspect and restore the exported file:

```bash
fieldctl transform inspect Activation-Field-Partial-Candidate.afwt
fieldctl transform materialize --base original.manifest.json --transform Activation-Field-Partial-Candidate.afwt --output restored.checkpoint
fieldctl session import --checkpoint restored.checkpoint --new-session --read-only
```

The model manifest is local and protected; the checkpoint ID is written to the installed service's state vault unless `--vault` is supplied. Source and target checkpoints are not distributed. `scripts/qualify_portability.py` in the repository reproduces the target-denied fresh-process check. CPU binary64 evaluation on decoded float32 region parameters is authoritative; no Metal transform evaluator or CPU/Metal equivalence is claimed.

## Remaining qualification

The uncapped full fit is ongoing and the actual-model spatial-only versus value-conditioned ablation has not finished. Larger candidates remain available; the packaged 11-region candidate is retained as a reproducible engineering example, not chosen as the best completed model. The parent Activation Field long-duration scientific experiment and validated memory/correction/preference probes remain pending, including the earlier descriptor accuracy shortfall. Continued-learning comparison is optional and unexecuted. Human usefulness assessment is pending.

The established development workflow was invoked, but its Orient occurrence remained in an expired-running/projection-pending state. Its completion was not fabricated. Implementation progress, native receipts, outstanding work and recovery identifiers are recorded in [Rhizome](https://tasker-workboard.cantikash.chatgpt.site/?task=47f9f416-2954-4223-950b-78504d135207) and Humus context `activation-field-functional-transform-20261105`.
