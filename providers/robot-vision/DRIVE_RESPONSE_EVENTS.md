# Drive response telemetry events

The passive monitor correlates acknowledged applied relay masks with unique,
timed IMU samples. It does not issue commands or trigger sensors. When enabled,
it requests a 20 ms cadence from the existing AHRS cache poller and samples that
cache independently of camera delivery. Capture-packet AHRS association remains
unchanged. Firmware currently samples the IMU at 50 Hz; the measured accepted
rate and coverage, rather than that nominal rate, qualify each assessment.

A drive interval includes a pre-drive baseline, onset acceleration and the
deceleration/settling window after drive ends. Same-direction hold acknowledgments
do not start new intervals. Neutral gaps up to 80 ms merge adjacent renewal
pulses. Forward and reverse use opposite signed longitudinal acceleration.
Quaternion gravity compensation precedes projection onto the configured rover
forward axis; the baseline removes residual bias.

A fully covered interval without a sustained onset or braking response emits
`robot.motion.no_drive_response`, classified `suspected_stuck_or_collision`.
This is a warning, not proof of no motion. Missing, repeated, stale or poorly
timed IMU samples yield an unassessable result. An isolated impact or ambiguous
vibration does not establish successful motion. Observed response clears an
earlier active event; repeated no-response intervals update that event.

Live frame metadata and `robot.ahrs.snapshot` expose `events` and
`drive_response`. Events carry IDs, lifecycle state, timestamps, pulse identity,
applied mask/sequence and measurement evidence. Raw sample history, pending
intervals and recent events are bounded in RAM; this monitor creates no durable
sample journal. No production event is injected during qualification.

Configuration is under `drive_response`: enable explicitly; provide the existing
local control subscription token file and the forward axis from the current
body model. Defaults are a 300 ms baseline/onset/settling window, at most 65 ms
between unique IMU samples, at most 60 ms clock uncertainty, 0.25 m/s² minimum
acceleration (raised by baseline noise), and 0.012 m/s minimum signed response
impulse. These conservative thresholds require physical qualification and may
be configured; they are not a measured rover response model.
