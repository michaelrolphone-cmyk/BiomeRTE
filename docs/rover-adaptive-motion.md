# Biome rover motion capabilities

The consumer interface is Biome's capability registry and invocation protocol.
Clients do not need an IP address, HTTP endpoint, dashboard session, separate
bearer token or a Heroku deployment.

| Contract | Inputs | Result |
|---|---|---|
| `robot.motion.model:1` | Optional `include_parameters: bool` | Model revision, retained steering/motion state, training support, residuals, freshness and optional portable parameter snapshot |
| `robot.motion.predict:1` | Required `pulses: list`; optional `initial_state: object`, `step_s: number`, `model_revision: integer` | Per-step trajectory/state estimates, support, uncertainty and explicit `executes_motion:false` |
| `robot.motion.plan:1` | Required `target: {distance_m, yaw_deg}`; optional `initial_state`, `constraints`, `model_revision` | Inverse pulse estimate, forward prediction, residual and support; never executes |
| `robot.motion.training.start:1` | Required `session_id`; optional `label`, `metadata` | Request a named manual-driving recording session |
| `robot.motion.training.status:1` | Optional `session_id` | Recorder health, captured events, accepted samples, rejection reasons and source freshness |
| `robot.motion.training.stop:1` | Required `session_id` | Stop the session and report whether queued observations have drained |
| `robot.motion.training.read:1` | Required `session_id`; optional `after_id`, `limit` | Page through retained controls/sensor evidence and sample qualification |

Discover or acquire the generic contract without specifying a controller or host.
The gateway selects an eligible implementation. A resource constraint is optional
when a caller needs the model for a particular robot; deployment placement is not
part of the contract name.

## Invocation

1. Discover `robot.motion.model:1`, `robot.motion.predict:1` or `robot.motion.plan:1`.
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
function. It does not proxy a local or public HTTP prediction API. All three contracts
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

## Inverse motion estimation

`robot.motion.plan:1` accepts a motion target and returns estimated pulse timings:

```json
{
  "target": {"distance_m": 0.4, "yaw_deg": 30},
  "constraints": {
    "distance_tolerance_m": 0.02,
    "yaw_tolerance_deg": 2,
    "max_pulse_s": 3,
    "max_total_s": 10,
    "allow_reverse": true
  }
}
```

`distance_m` is signed travelled distance, not endpoint displacement. `yaw_deg`
is unwrapped heading change, positive left. Both are required, including zero.
An in-place turn is not assumed possible. The planner searches drive/steer
sequences and refines their durations through the same forward model. Every
candidate starts from the retained or explicitly supplied initial state, then
carries state continuously through all its pulses; steering is never reset
between candidate segments. Inversion does not update the learned model.

The result contains `pulses`, the full forward `prediction` (including uncertainty
proxy and final state), signed `residual`, `within_tolerance`, model identity,
actual evaluation count and constraints. `insufficient_calibration` yields null
pulses; `best_effort_outside_tolerance` must not be mistaken for reaching the
target. `no_supported_plan` describes the searched envelope, not a global proof
of infeasibility. Initial-state uncertainty remains explicit.

Search defaults are min/max pulse 0.05/3 seconds, total 10 seconds, 4 pulses,
beam width 8, 600 forward evaluations and integration step 0.1 seconds. All are
caller-configurable through `constraints` (`min_pulse_s`, `max_pulse_s`,
`max_total_s`, `max_pulses`, `beam_width`, `max_evaluations`, `step_s`,
`allow_reverse`, `distance_tolerance_m`, `yaw_tolerance_deg`). Only masks supported
by both observed distance and yaw models are searched. This is approximate
bounded optimization, with no global optimum, obstacle clearance or terminal
stop guarantee. Re-estimate after executing and observing a segment; this
capability itself never sends control commands.

The same inverse is callable without Biome transport:

```python
from biomerte.motion_planning import plan_motion
plan = plan_motion(model, {"distance_m": 0.4, "yaw_deg": 30}, initial_state=state)
```

Run inverse and regression coverage with:
`python -m unittest discover -s tests -p 'test_motion*.py' -v`.

## Manual driving training

Start `robot.motion.training.start:1` with
`{"session_id":"room-drive-001","label":"Manual room navigation","metadata":{"battery":"fresh"}}`.
Drive normally with the existing manual controls, then call
`robot.motion.training.stop:1` with that session ID. The session does not acquire
physical control, send driving commands, or change who can view/control streams.
Stopping recording does not stop the rover; its normal stop control still applies.

Inspect `robot.motion.training.status:1` before and during driving. `recording`
means the passive writer acknowledged the session and has a recent heartbeat.
`recorder.learning_inputs_ready` separately checks recent applied controls and
AHRS capture receipts. Idle sessions can be waiting for controls. Event counts,
accepted angular/metric samples and rejection reasons distinguish a retained
recording from usable model training. Stale/missing sensors, ambiguous control
application, source/reference gaps and unqualified sonar geometry remain explicit.
Qualified intervals update the existing online learner; this is motion-response
training, not a claim that the rover has learned a navigation policy.

Session IDs are stable idempotency keys. Repeating start with identical metadata
returns the existing session, including one already stopped; it never reopens it.
Different metadata for the same ID and simultaneous distinct active sessions are
rejected. Repeating stop is safe. A session survives observer restart, with a
recorded continuity gap; first-frame association never crosses a session boundary.
After stop, wait for `session.drained` before treating the dataset as complete.
`training.read` returns initial model/state, event metadata and per-event learning
acceptance. Follow `next_after_id`; active/draining sessions can still grow.

Only controls, AHRS/sonar, pose/context metadata and qualification are retained;
video image bytes are not recorded. Named-session records and all legacy driving
evidence are preserved. New unlabelled telemetry uses a rolling 120-second raw
buffer with bounded pruning work, so leaving the observer on does not retain
an endless raw sensor history. Training sessions are not automatically deleted.
The AHRS null-reference failure is handled and a writer heartbeat/watchdog prevents
a dead recorder from continuing to advertise a live recording.
