# Live rover motion events

Every independent camera frame publishes motion_events (schema
biome.rover.events.v1) and appends the same identified events to the existing
top-level events list. Existing drive_response telemetry is preserved. Consumers
deduplicate by event_id; the recent event ring holds at most 64 entries.

Event types: collision (suspected horizontal impact at rest or opposing impact during applied drive),
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

Deployment must include the repository biomerte package alongside providers so the passive subscriber can use the shared stream codec.

Stream event types use robot.motion.collision, robot.motion.stuck,
robot.motion.turning and robot.motion.pickup; kind is the short name. The common
event state is active/cleared, with the detailed phase and direction retained.

To fit the existing wire header, stream AHRS history is encoded losslessly as
imu_history {schema, columns, rows}. Column names include units and each row
preserves all 16 fields. Internal capture packets keep imu_samples objects.
The stream has one full event list at top-level events, with references under
motion_events. It resends up to eight recent events; additional older omissions
are explicit in event_delivery. robot.vision.status retains the 64-event recovery
ring. Stream header overflow logs per-field byte sizes instead of failing silently.

The stream publishes the supported fully qualified names in motion_events.event_types. A fresh, classified frame sets state.turning to a boolean and state.turn_direction to left, right, or null. With invalid attitude, a quiet capture-aligned raw gyro can still rule out a turn. A faster gyro alone cannot establish turn direction. The last applied relay acknowledgment appears in drive_response.last_applied. A pickup updates tof.ground_contact.last_pickup_at on the same live frame.
