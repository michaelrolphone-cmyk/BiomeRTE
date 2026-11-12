# Biome rover motion capabilities

The consumer interface is Biome's capability registry and invocation protocol.
Clients do not need an IP address, HTTP endpoint, dashboard session, separate
bearer token or a Heroku deployment.

| Contract | Inputs | Result |
|---|---|---|
| `robot.motion.model:1` | Optional `include_parameters: bool` | Model revision, retained steering/motion state, training support, residuals, freshness and optional portable parameter snapshot |
| `robot.motion.predict:1` | Required `pulses: list`; optional `initial_state: object`, `step_s: number`, `model_revision: integer` | Per-step trajectory/state estimates, support, uncertainty and explicit `executes_motion:false` |

Discover or acquire the generic contract without specifying a controller or host.
The gateway selects an eligible implementation. A resource constraint is optional
when a caller needs the model for a particular robot; deployment placement is not
part of the contract name.

## Invocation

1. Discover `robot.motion.model:1` or `robot.motion.predict:1`.
2. `cap.acquire` with the contract and a unique task identity.
3. Read `cap.lease` until bound.
4. `cap.invoke` with the lease, a unique operation identity and inputs.
5. Read `cap.status` for that same operation; the telemetry is under
   `result.result`. Inspect an explicit query error before using the result.
6. Release the lease. `cap.submit` is also supported when using a discovered
   resource; follow its returned operation to completion.

Prediction inputs:

```json
{
  "pulses": [
    {"mask": 5, "duration_s": 0.8},
    {"mask": 0, "duration_s": 0.3},
    {"mask": 1, "duration_s": 0.7}
  ]
}
```

Mask bits are 1 forward, 2 reverse, 4 left and 8 right. This estimates a turn,
neutral and subsequent straight-drive request. Opposing drive or steering pairs
are rejected. The predictor never sends these commands to the robot.

The steering latch remains turned through neutral and straight drive until
corrective steering is applied; release is not treated as centered wheels.
Four transient memory features let the response estimator distinguish settling
from persistent steering. These are latent features, not measured wheel angles.
Signed speed/yaw, drive history and on/idle times contribute to the adaptive
regression. Time-based forgetting and residual-triggered covariance inflation
allow response changes due to power, payload or traction to be learned rather
than freezing the initial fit.

Unsupported/untrained outputs are null. A query completing successfully is not
physical calibration. The uncertainty field is an observational residual/leverage
proxy, not a calibrated confidence interval or collision-clearance guarantee.
A caller can pin `model_revision`; a mismatched revision returns
`model_revision_conflict`. Stale capture/model data makes live motion state
explicitly unknown. A caller may supply an explicit initial-state assumption.

## Architecture and ownership

The observer consumes existing multiplexed control/video streams and independently
reads sonar and coherent cached capture snapshots. It owns no physical lease and
issues no motion commands. Raw fast-frame AHRS/sonar headers are retained separately
from snapshots with capture-host clock bounds. Requested/application feedback,
source identities, sensor/reference resets and timing uncertainty are distinct.

A dedicated native capability worker has its own provider identity, work inbox,
fenced execution ledger and parallel action pool. It reads a consistent,
read-only SQLite model snapshot and calls the same reusable `MotionModel.predict`
function. It does not proxy a local or public HTTP prediction API. Both contracts
are read-only; their leases protect execution ownership, not physical driving.

The live observer database is
`~/.local/share/biome-rover-motion/motion/observations.sqlite`.
Provider configuration/ledger is separately stored in
`~/.local/share/biome-rover-motion/provider`.
The gateway assigns implementation authority from the provider's reported locality;
neither caller instructions nor contract names require Mac/Pi placement.

The observer's records and model survive restarts. Prior steering history is
retained while gap/restart uncertainty remains explicit. No old motion command
is replayed. Duplicate/reordered ACKs, missed output transitions, camera boot or
AHRS reference changes, stale samples and ambiguous actuation windows cannot
train as if their physical state were known. Metric fitting additionally checks
opposed front/rear sonar changes at nearly constant heading. Sequential beam
measurements and target geometry remain limitations.

## Reusable estimator and verification

```python
from biomerte.motion_model import MotionModel
prediction = MotionModel(saved_parameters).predict(pulses, initial_state=state)
```

The native provider exposes this function through `robot.motion.predict:1`.
`include_parameters:true` on the model contract supplies an immutable snapshot
for offline estimation or another implementation.

Run `python -m unittest tests.test_motion_model tests.test_motion_capabilities -v`.
Verification must include actual generic discovery, lease binding, invocation,
status and release through Biome; local function or HTTP tests alone do not
establish capability delivery. No driving tests are implied by these read-only
checks. Ordinary qualified driving observations must establish physical
predictive accuracy.

Install a new worker with `python providers/rover-motion/install.py --resource ROBOT_MODEL_RESOURCE` after the observer is installed. The installer uses the current native worker runtime (including settlement-proof handling), reuses the existing managed gateway transport, and retains the observer/model runtime separately. An existing provider must be reconciled before replacement; camera and drive services are not restarted.
