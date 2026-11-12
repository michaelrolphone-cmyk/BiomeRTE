# Observational rover motion response

The predictor is read-only. It neither acquires control leases nor drives a
calibration maneuver. Normal driving supplies the experiments.

## State and model

The state contains signed speed, yaw rate, prior drive direction, continuous
on/idle times and four latent steering-memory traces (0.15, 0.5, 1.5 and 5 second
basis time scales). These traces are features, not measured steering angles or
calibrated settling constants. Neutral at rest preserves them. Straight rolling
allows gradual recentering; reversing retains the physical steering history.
An adaptive autoregressive regression learns signed speed and yaw response from
those features. Time-based forgetting and residual-triggered covariance inflation
allow the observed response to change as power, payload or traction changes.
No battery-voltage measurement or wheel encoder is invented.

The model is an observational estimator, not a certified vehicle dynamics model.
Nonlinear behavior is represented by memory/input interaction features; unobserved
steering/drive combinations are explicitly flagged. Strong extrapolation and
different floor/contact conditions remain unqualified. Additional encoder,
battery-voltage or steering-angle sensors can be incorporated from the raw log.

## Recording and qualification

`install_motion(rover, authenticated_app, home)` adds an independent recorder thread.
Control dispatch and frame forwarding do not wait for fitting or SQLite writes.
The SQLite file is `home/motion/observations.sqlite`; raw applied/requested events,
sensor metadata, source identities and clock bounds are retained, with model
snapshots in the same transaction. Image pixels and geometry clouds are excluded.

Requested commands and applied ACKs are distinct records. ACK receive time is
not labeled GPIO time: issue-to-receipt brackets and provider monotonic stamps
are retained. Duplicate/reordered ACKs, connection changes, stale frames, source
boot changes, AHRS reference resets and ambiguous actuation windows cannot train
the response model. Ambiguous windows still update uncertain steering history.

AHRS readings use their capture-associated attitude and validity/age fields.
The independent cache-only sonar reader logs all four ranges and actual sample
times. Readings are associated to earlier camera captures only within the same
device boot and documented sensor-period age. Metric training requires agreement
between opposed front/rear range changes and little heading change. Sequential
sonar samples and beam/target geometry still limit this translation hypothesis;
a changing range alone is never labeled traveled distance. Missing metric support
returns null metric predictions.

The process retains the learned response and steering memory across restarts,
but marks live speed and steering continuity unknown after a restart/disconnect.
No old command is replayed. The full source log permits later offline replay and
improved synchronization/fitting without recollecting every experiment.

## Endpoints and function

On the authenticated paired Rover HTTP app:

* `GET /api/motion/model`: live estimated state, model revision, sample/domain
  support, rejection reasons, residuals, drift counters and recorder health.
* `POST /api/motion/predict`: simulate a sequence from current or supplied state.

Example request:

```json
{"pulses":[{"mask":5,"duration_s":0.8},{"mask":0,"duration_s":0.3},{"mask":1,"duration_s":0.7}]}
```

Masks: 1 forward, 2 reverse, 4 left, 8 right; combine drive and steer.
The example predicts forward-left, neutral, then straight driving while retaining
the prior steering state. Opposed drive or steering pairs are rejected.
Optional `initial_state` overrides explicit estimated state fields. Optional
`step_s` controls numerical integration. Every response includes
`executes_motion:false`, angular/metric availability, unsupported combinations,
per-step state and accumulated trajectory, and an uncertainty proxy. That proxy
is not a calibrated confidence interval or collision-clearance guarantee.

Reusable function:

```python
from biomerte.motion_model import MotionModel
model = MotionModel(saved_model)
prediction = model.predict(pulses, initial_state=state)
```

Predictions do not update the live model. `MotionRecorder.predict` preserves the
current in-process state when taking an immutable prediction snapshot.

The public stream gateway must allow the two new HTTP paths before remote
consumers can use them. Existing gateway auth remains required. Adding local
routes alone does not prove public delivery. Deployment records must distinguish
local integration, public endpoint verification and physical predictive accuracy.

## Deployment and verification

`python deploy/enable-rover-motion.py --apply` snapshots the currently deployed
Rover package and adapters, overlays only these modules and the app attachment,
and records the prior launch-agent file for rollback. It preserves existing
clock/stream repairs, maps and camera/vision service. Inspect the fresh production
service identity before deploying if another worker changed it.

Run `python -m unittest tests.test_motion_model -v`. Tests cover retained steering,
recentring, history-dependent predictions, read-only estimation, missing metric
data, adaptation after a response shift, restart continuity and ambiguous windows.
Software tests do not qualify physical prediction accuracy. This starts with no
fabricated calibration coefficients; ordinary qualified observations build support.
