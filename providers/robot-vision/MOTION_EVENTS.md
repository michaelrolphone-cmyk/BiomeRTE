# Live rover motion events

Every independent camera frame publishes motion_events (schema
biome.rover.events.v1) and appends the same identified events to the existing
top-level events list. Existing drive_response telemetry is preserved. Consumers
deduplicate by event_id; the recent event ring holds at most 64 entries.

Event types: collision (suspected opposing horizontal impact after drive onset),
stuck (suspected missing acceleration response), turning (started/ended, left or
right), and pickup (inertial lift estimate, or suspected loss of support).
Positive gravity-axis angular velocity and increasing relative yaw mean left.
Absolute compass calibration is unavailable; relative yaw can drift.

Classification uses the camera's causal, overlapping 50 Hz IMU history, not
single 5 Hz samples. Drive assessment requires actual applied acknowledgments,
a known preceding control state, bounded host clock uncertainty, continuous IMU
coverage, and a stationary sensor bias baseline. Queued requests, hold renewals,
gaps, calibration changes, stale data and reconnects cannot establish a new
motion failure. Flat acceleration does not prove zero velocity.

Collision, turning and pickup normally appear on the next 200 ms camera frame
after their evidence criteria are met. Missing drive response requires at least
650 ms for an ongoing drive or 250 ms after the applied stop. The preserved
pulse-curve assessment includes onset and braking windows. Timing is an
observation target, not a hard end-to-end delivery guarantee.

The passive control reader never acquires control or sends outputs. The motion
observer's coherent snapshot reads are triggered by incoming vision frames;
AHRS is recorded directly from the matching frame. No 250 ms AHRS timer remains.
