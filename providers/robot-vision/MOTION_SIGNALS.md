# Passive motion-window signals

`motion_signals.py` turns repeated AHRS observations into source-bound features. It does not issue commands, change firmware, declare free space, or provide calibrated vehicle pose.

Run against recorded windows:

```sh
python motion_signals.py trial-1.json trial-2.json
python motion_signals.py --response-model trial-1.json trial-2.json
python -m unittest discover -s tests -p test_motion_signals.py
```

The CLI writes JSON Lines; the response-model option writes one grouped evidence ledger. These outputs can be consumed by a driving evaluator without repeating manual numeric calculations. No live control behavior changes until a consumer explicitly adopts and qualifies them.

## Inputs and interpretation

A generic window supplies `boot_id`, `sample_id`, `frame`, `time_s`, `accel_m_s2`, `gyro_rad_s`, `valid`, and `age_upper_bound_s`. The baseline supplies the same frame and boot, acceleration and gyro vectors, validity/freshness, and an explicit `stationary_verified` flag. An optional `gyro_reference` identifies the correction/calibration epoch; changes break the window.

The recorder adapter reads actual IMU `observed_uptime_ms`, never envelope uptime or frame receipt time as a substitute. It adds firmware sample age, host time since receipt, and request duration to bound age conservatively. Repeated boot/sample-count pairs contribute once. Boot, frame or gyro-calibration changes and stale/malformed/out-of-order samples are rejected. A same-boot uint32 millisecond wrap is unwrapped only when the forward modulo delta is below half the clock range.

Measured stationary specific force defines local up. The projected angular rate is `(gyro - baseline_gyro) dot up`; trapezoidal integration uses only adjacent observed intervals no greater than 0.5 seconds by default. A longer gap contributes no invented rotation. `observed_yaw_delta_deg` covers those intervals only. `window_yaw_delta_deg` is null when coverage, freshness, sensor identity or independently verified stationarity is insufficient. The sign is about measured up; it is not yet vehicle left/right or a compass heading.

The acceleration magnitude departure `abs(norm(a) - norm(baseline_a))` is invariant under sensor orientation. Departures above the provisional 2 m/s² threshold are **jolt candidates**. It can miss lateral or short impacts, and wheel acceleration, floor joints or vibration can create candidates. Vector acceleration change rate is retained separately and can include gravity changes during pitch/roll. Neither signal proves a collision. Absence does not prove clearance or motion. Distance from double integration is deliberately unavailable.

Stationary-baseline subtraction handles a local bias estimate; it does not prove persistent gyro calibration. The trial adapter defaults to unverified stationarity because one pre-command sample cannot establish it. Callers must verify a stationary window independently before supplying `stationary_verified=True`.

Command, host receipt and firmware clocks are not qualified as one capture-time clock. The response ledger retains accepted-to-applied **acknowledgment arrival** delays, separately from physical response delay, which remains null. Camera alignment and body mounting transform also remain unqualified.

## Recorded evidence, 2026-11-11

`models/rover_motion_evidence_20261111.json` pins ten raw trial files by SHA-256. The recorded windows are sparse snapshots of the 50 Hz firmware sensor, not a lossless 50 Hz stream. All automated summaries remain provisional because their stationary baselines were not independently qualified.

| Requested input | Case | Observed interval rotation | Limits |
|---|---|---:|---|
| Forward + right, 1.5 s | 11 | -24.61° | Gaps/rejected stale sample; fused yaw changed about -28° |
| Forward + right, 0.6 s | 28 | -10.44° | Complete sampled window; unverified baseline; two jolt candidates |
| Forward + left, 0.9 s | 24 | -2.49° | Coverage gap; one 3.22 m/s² magnitude departure |
| Reverse + left, 0.8 s | 25 | -2.95° | Gaps/rejected sample; one jolt candidate |
| Reverse + right, 1.1 s | 26 | +8.09° | Coverage gap |
| Reverse + right, 1.1 s | 27 | +0.19° | Gaps/rejected sample; no proof of motion or obstruction |

Repeated commands have not produced a qualified repeatable steering model. Wheel transition, rolling requirement, resource interruptions, possible obstructions and delayed physical response are confounders. Do not fit turning radius or motor labels from this ledger. A successful acknowledgment proves commanded output state, not chassis movement. Continue comparing actual observed signed rotation and image displacement after each bounded maneuver.

Camera boot changed between case11 (`c2e8382e39dd0468`, firmware bias applied) and cases24–28 (`7907e187a930cb46`, bias unapplied). The later approximately `[-0.03, 0.006, -0.043] rad/s` stationary raw gyro can cause large accumulated relative-yaw drift. Compare local bias-subtracted windows, not fused yaw across long pauses or boots.

## Qualification still required

Collect repeated independently stationary-baselined windows with stable sample delivery, synchronized command/sensor time bounds, known body-axis alignment, and observed chassis movement. Label real contacts and non-contact transients to qualify thresholds. Until then this is a reusable evidence processor and ledger, not an automatic collision detector or steering controller.
