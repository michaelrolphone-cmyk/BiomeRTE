# Activation Field — Qwen3.5 9B

Experimental native Apple Silicon MLX service and Biome agent. All 32 decoder layers and 248 eligible projections participate. Working packed 4-bit values can change between token evaluations. The pinned model files, scales, offsets, embeddings and other frozen tensors remain unchanged. Baseline pull defaults to zero and is adjustable.

## Use through Biome

Discover `agent.activation_field.delegate:1`, acquire its offered resource and invoke using a unique operation ID, as for other Biome capabilities. Inputs:

```json
{"request":"Reply with one word: ready.","context":{"config":{"enabled":true,"baseline_pull":0},"max_tokens":8}}
```

The response supplies `session_id` and `version`. Include both as `session_id` and `expected_version` for subsequent operations. Explicitly set `persist:true` at creation to keep the session across service restarts. The default field is disabled until `config.enabled=true` is selected. Normal Biome agent results use the ordinary controller result path; this is not the Android private-chat encrypted transport.

Management actions in `context.action`: `health`, `diagnostics`, `config`, `reset`, `checkpoint`, `fork`, `import`, `delete`. Reset kinds: `ordinary_state`, `activity_traces`, `weights`, `entire_session`. Config is a flat object from `configs/initial.json`. New `import` calls take `checkpoint_id` and start independently with empty native caches by default.

For single `request` turns, the adapter explicitly clears ordinary caches before each subsequent turn while retaining field weights. To preserve ordinary conversation context, supply the exact complete transcript in `context.messages`. Editing an existing prefix is rejected; reset or fork explicitly. A cancelled prefill requires ordinary-state reset.

## LM Studio

Install the local generator from `lmstudio-generator` with `lms dev --install --yes --no-notify`. Select `biome/activation-field` as the generator and choose a unique **Field session** name in that chat's settings. Reusing a name deliberately selects its existing field. Enable **weight writes** when wanted; **Baseline pull — 0 is Off** stays at zero initially.

The plugin is installed on the Mac. Its external SDK verification is pending because the current LM Studio API token lacks permission to use this plugin; the Biome agent calls the native service directly and has passed live generation.

Controls include learning rate, amplitude/frequency gates, half-life, commit interval, role scales and read-only mode. Chat commands: `/field diagnostics`, `/field save`, `/field load CHECKPOINT_ID`, `/field fork`, `/field reset ordinary`, `/field reset weights`, `/field reset traces`, `/field reset all`. Management turns are excluded from model input. Reset/import/fork start the following conversation segment without silently replaying old content into the field.

## Native service and lifecycle

Installed root: `~/.local/share/biome-activation-field`. The Python environment is `.venv`; source is `source`. `fieldctl` is in `.venv/bin`. API binds only `127.0.0.1:8791`, authenticated using the owner-only `api-token` file. It exposes session creation, streamed/nonstreamed generation, request status/cancel, versioned config changes, diagnostics, checkpoint/fork/import, and deletion.

`python install.py` installs `org.biomerte.activation-field-api` and `org.biomerte.activation-field-agent` LaunchAgents. To stop, `launchctl bootout gui/$(id -u)/org.biomerte.activation-field-agent` and the corresponding `activation-field-api` label. Start again using `python install.py` in the package environment.

The service holds no model while idle. Each model operation acquires the existing shared model/image FIFO, loads this model, saves state, unloads, then releases admission. Existing LM Studio and image providers keep their normal scheduling. No inference wall-time limit is added.

## Reproduce

```bash
fieldctl model prepare --source mlx-community/Qwen3.5-9B-4bit --revision 8b2b98c00a6b4d291155e4890773ca8f769aee53 --bits 4 --group-size 64
fieldctl model inspect --manifest ../model-manifest.json
fieldctl serve --manifest ../model-manifest.json --config configs/initial.json
python -m pytest -q
python tests/qualify_full_model.py
fieldctl experiment run --config configs/experiment.json --resume
fieldctl experiment report --run ../experiments/zero-pull-42 --output ../experiments/zero-pull-42/report
```

Run from the installed source directory with its Python environment. Do not run `serve` manually while its LaunchAgent already owns the port. Experiments use the same exclusive scheduler and may take days at full settings.

## Checkpoints and future transforms

Encrypted AES-GCM chunks with authenticated tensor metadata, SHA256 checksums, atomic publication, and explicit baseline identity. A local owner-only random key protects stored sessions; there is no custom password scheme. Complete checkpoints include hybrid recurrent/convolution and rotating KV state, positions, weights, traces, sampling counters, configuration and trajectory state. Field-only exports exclude ordinary caches.

`FieldStore` exposes `enumerate_state`, `iter_changes`, `export_changes`, `materialize_state`, and `import_field`. Group differences are tiled and carry exact replacement codes plus signed numeric deltas. Exporting a saved checkpoint does not change the active field. Generalized transform fitting and clustering remain deferred.

## Evidence and limitations

See `evidence/qualification.json` and `HANDOFF.md` for observed results, exact deployment state and unrun experiments. Engineering correctness does not establish useful memory. The C3/C4 matched-direction/replay controls are explicitly unexecuted until their exact synthetic receipts are available. Trajectory descriptors are unvalidated measurements, not correctness or confabulation detectors. Optional persistent homology is not enabled. Never interpret an unrun condition as a pass.



## Durable experiments

`POST /experiments` accepts `request_id`, `expected_version: 0`, and `config` (the experiment fields shown in `configs/experiment-confirmatory.json`, excluding `manifest` and `output`, which the service supplies). It returns a durable `run_id`. Poll `GET /experiments/{run_id}` for state, completed trial count, current condition/seed/episode/gap and phase. Restart interrupts in-flight runs visibly; resume by POSTing a new `request_id`, that `run_id`, and its current `expected_version`, without a new config. Reusing a request ID with changed content is rejected. Identical retries return the original receipt.

The CLI and service acquire the same per-run file lock. Resume requires exact config, baseline and corpus identity; an interrupted trial restarts from the immutable baseline while completed trials remain preserved. Model admission is acquired per trial. No execution timeout is added. Confirmatory trials are computationally expensive; progress and reports distinguish partial/unexecuted conditions from successful measurements. C3/C4 remain explicitly unexecuted pending exact matched replay.

Job configuration chooses `kind`: `memory` (default), `stability`, or `descriptors`. Example configurations are in `configs/stability.json` and `configs/descriptors.json`. The stability runner defaults to 10,000 tokens and checkpoints every 512, releasing model admission between checkpoints. The descriptor runner derives correctness labels from executable oracles, splits templates before measurement, trains only on training data, and calibrates on validation data. Both save progress and an explicit result file; failure is visible and resume is explicit.

Through Biome, use `context.action=experiment_start` with `context.experiment_config` (omit manifest/output), then `context.action=experiment_status` with the returned `run_id`. Starting a job returns immediately. For explicit resume, send `experiment_start`, `run_id`, and the reported `expected_version` with a new operation ID.

Optional topology: install `.[topology]`, then set `persistent_homology=true` in a descriptor run or field configuration. H0/H1 use the flag complex of `1-S/max(S)`, without assuming a metric. [Ripser API](https://ripser.scikit-tda.org/en/latest/reference/stubs/ripser.ripser.html). Its utility is measured against the graph-only baseline.

Native regressions: `python -m pytest -q`, `python tests/qualify_full_model.py`, `python tests/qualify_continuation.py`, and `python tests/qualify_api.py`. Qualification reports keep skipped, failed, unexecuted and measured results distinct.
