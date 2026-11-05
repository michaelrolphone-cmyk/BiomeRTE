# Functional weight transforms

The native Activation Field service can fit an immutable evolved checkpoint to a
portable function, restore it against the original model and compare the three
states. This extends the existing full Qwen3.5 9B runtime; no LM Studio interface
is required. It does not automatically replace an active conversation's weights.

## Biome capabilities

All names below have version `:1` and input `{"arguments": {...}}`. The Biome
operation ID supplies `request_id`. Results are under `result`; check `ok` and
the durable job's `status`. Starting a job is not completion.

| Capability | Arguments |
| --- | --- |
| `model.field_transform.fit` | `expected_version: 0`, `target_checkpoint`, optional `config` |
| `model.field_transform.status` | `run_id`, optional `details: true` for the complete report |
| `model.field_transform.inspect` | fit `run_id`, `candidate_id` |
| `model.field_transform.export` | fit `run_id`, `candidate_id`; returns the complete AFWT through checksum-verified Biome artifact parts |
| `model.field_transform.materialize` | `expected_version: 0`, `fit_run_id`, `candidate_id` |
| `model.field_transform.compare` | `expected_version: 0`, `target_checkpoint`, `restored_checkpoint`, `suite` |
| `model.field_transform.cancel` | `run_id`, current `expected_version` |
| `model.field_transform.trial` | completed comparison `run_id`, current `expected_version`, optional `blind` |
| `model.field_transform.assessment` | completed comparison `run_id`, current `expected_version`, `observations`, `attribution`, optional `selected_candidate` and `blind_labels` |

Resume failed, interrupted or cancelled fit/materialize/compare jobs using the
same capability with `resume_run_id`, current `expected_version`, and a new Biome
operation ID. Inputs remain frozen. An identical retry returns the original
receipt; reusing its ID with changed content fails. Cancel requests stop at a safe
boundary. An OS file lock survives worker startup; a missing worker is reported
as interrupted. There is no job timeout or default region-count limit.

Export responses use `encoding: biome-artifact-tree/1` and an `artifact` reference.
Call `artifact.get` for `artifact.root.sha256`, decode the returned base64, and
parse that JSON manifest. A `kind: chunks` node lists raw parts to concatenate in
order; `kind: nodes` lists child manifests to traverse in order. Verify each
part's SHA-256/size and the final `artifact.sha256`/`artifact.bytes`. The reusable
`transforms.transport.read_transfer(reference, get_bytes)` performs this check.
Large status reports use the identical protocol under `result_artifact` with
`result_encoding: biome-artifact-json/1`; decode the reconstructed JSON for the
complete result. Small results remain inline. Transfers use 32 KiB frames and
128-way manifests to fit the observed gateway request ceiling; they impose no
limit on the complete function, candidate registry, or report. Retried uploads
use immutable content hashes. Local HTTP export still returns complete base64.

`trial` creates independent, read-only Baseline, Evolved and Restored sessions.
With `blind: true`, it returns A/B/C labels and keeps the assignment encrypted.
Choose a label and call `agent.activation_field.delegate:1` with the returned
session ID/version. Each new trial starts with empty native state. Returning to
Baseline means selecting its independent session. Assessment records the stated
attribution; it never manufactures Michael's endorsement.

Example comparison suite (illustrative, not measured results):

```json
{"probes":[{"id":"arithmetic","prompt":"Reply with only the answer: 2 + 3 =", "continuation":" 5", "expected":"5", "max_tokens":8,"kind":"general"}]}
```

The additive sidecar binds loopback port 8793 and uses the existing field token.
It can be installed without restarting the field API or agent. Routes are
`POST /transforms/{fit,inspect,export,materialize,compare}`,
`GET /transforms/jobs/{run_id}` and
`POST /transforms/jobs/{run_id}/{cancel,trial,assessment}`.

## Command line

```bash
fieldctl transform fit --base original.manifest.json --target CHECKPOINT_ID --config fit.json --output fit-run
fieldctl transform candidates --run fit-run
fieldctl transform export --run fit-run --candidate CANDIDATE_ID --output context.afwt
fieldctl transform inspect context.afwt
fieldctl transform materialize --base original.manifest.json --transform context.afwt --output restored.checkpoint
fieldctl transform compare --base original.manifest.json --target CHECKPOINT_ID --restored restored.checkpoint --suite probes.json --output comparison
fieldctl session import --checkpoint restored.checkpoint --new-session --read-only
```

`--target` and `--restored` are immutable checkpoint IDs in `--vault` (default:
the installed service's `state` directory). Materialize uses the basename of
`--output` as its checkpoint ID in that vault and a sibling pending directory for
staging. `--resume` is explicit. A minimal `fit.json` is `{}`. See
`activation_field.transforms.fit.DEFAULTS` for every accepted control.

## Representation and fitting

AFWT is a deterministic ZIP_STORED containing exactly `manifest.json` and
`regions.bin`. Regions are 48-byte little-endian `<II9f4B` records. The evaluator
uses normalized tensor cell centers, baseline RMS value normalization, signed
rotated ellipses, optional affine value dependence and compact value windows.
Exponents are 1, 2 or 4. Contributions add in increasing region-ID order and all
read the original baseline value. Reference arithmetic is binary64 using decoded
float32 parameters. There are no hidden sampled weights or residual shards.

The fitter streams all tensors and records signed statistics. Deterministic
coordinate hashing separates training, validation and final diagnostic data.
Temporary stratified Bernoulli samples carry explicit inclusion probabilities;
the normalized changed and unchanged losses remain separate. Global coordinate
moments permit regions broader than a tile. Residual partitions propose signed
regions; least squares refits overlap neighborhoods; deterministic coordinate
search refines geometry and value gates; decoded validation chooses improvements
and prunes redundant contributions. The run retains candidates and a measured
size/fidelity frontier with geometric penalty selections. Size is a preference:
one million bytes is not a rejection or stopping threshold.

Defaults expose numerical convergence (`1e-4` relative loss improvement and
`0.05` geometry search tolerance), sampling workspace fraction (`0.001`) and
memory-derived tiles. These are declared optimization/workspace policies, not
scientific quality gates. Empty strata and unobserved validation strata are
reported. Constant-height fitting is selected with `value_modes: ["disabled"]`;
the default also explores affine and windowed affine proposals. Unpenalized
accepted candidates remain available above the size goal.

Restoration checks exact baseline identity and tensor/quantization mapping,
uses fixed original scales/offsets and ties-to-even rounding, and preserves
baseline codes for zero delta or zero scale. BF16 metadata is promoted exactly
before NumPy arithmetic. The pinned MLX model also contains negative affine
scales. Those artifacts explicitly declare the extension
`affine4-native-signed-scale-binary64-ties-even-v1`: invert the original signed
scale and round in native code space. Positive-scale artifacts retain the spec
rule. Scales, offsets and packing remain unchanged; the native regression test
compares dequantization to MLX and checks a negative-scale half-step.
Encrypted per-tensor staging publishes only a complete
checkpoint. Restore never requires the evolved target or fitting observations.

## Protection and evidence

Candidate functions, observations, inputs, reports and checkpoint fragments use
the existing authenticated encryption and local secret handling. Explicit export
creates the portable clear AFWT; exported bytes are sensitive derived state.
Directories are private and source checkpoints are never overwritten.

Jobs use the same FIFO model/image admission as existing agents. The model is
released when work ends. Numerical reports distinguish full streamed measurements
from sampled validation scores. Numerical `rmse`/`mae` describe the real-valued fitted delta;
`quantized_rmse`/`quantized_mae` and their changed/unchanged variants describe
the actual restored quantized weights. `relative_delta_error` also uses the
restored quantized weights. These stages remain separate because rounding can
erase small fitted changes. Comparison uses identical teacher-forced prefixes
for KL, separate free-generation trials, fresh native state for every probe, no
writes and no baseline decay. It records raw B/T/R scores, paired uncertainty and
an undefined retention ratio when its denominator is zero. Small denominators
must be interpreted with the raw scores and uncertainty, not as stable retention.

Passing mechanics does not demonstrate useful model memory or compression near
1 MB. Consult the [native qualification report](qualification/README.md) for what
was actually executed. The full parent experiment, optional continued-learning trial and any
unexecuted matched-noise controls remain separate scientific work. CPU binary64
is the only transform evaluator delivered here; no Metal equivalence is claimed.
