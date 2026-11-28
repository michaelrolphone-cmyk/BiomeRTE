# Camera, AHRS and four-sonar mapping

The service uses capture-clock attitude increments to seed pySLAM's rotational
motion prediction, then combines the visual orientation with the AHRS prediction.
Visual translation is combined with a separate local metric sonar odometry track. A lost visual pose does not become a
stationary pose just because orientation is available. Large visual orientation
innovations re-anchor the filter, preserving relocalization and loop closure.

The rover's configured IMU-to-camera rotation uses the owner's three observed
motions: left turn increases yaw (-105 to -60 is +45 degrees), front-up decreases
roll, and right-side-up increases pitch. In camera right/down/forward axes this
is `R_camera_imu = [[-1,0,0],[0,1,0],[0,0,-1]]`. The approximately 4mm centered
rearward AHRS position is recorded separately from this axis transform. The
default service uses this rotation at startup; passive axis learning is a fallback
for an explicitly unconfigured replacement mount, not a required manual step.

Gyroscope bias calibration is automatic. The independent raw camera callback
checks two distinct, fresh device captures for unchanged imagery and quiet IMU
readings, then requests the firmware's two-second bias measurement on a background
worker. It never waits on SLAM restoration, blocks mapping or blocks video.
Duplicate/replayed captures cannot trigger calibration. Fresh firmware telemetry
reconciles success, collection, motion rejection and retry after another stationary
interval. An existing valid bias is used immediately; a reboot re-arms the check.

Firmware adds four-sonar history to `biome.capture.v1`. Each transducer retains
its own transmit/echo midpoint measurement time and completion time, on esp_timer.
The capture packet selects only returns both measured and completed before the
image capture. Missing history stays unavailable. Separate latest HTTP telemetry
and the independent latest-image live feed keep their existing behavior.

The host uses causal sonar ranges for live telemetry and metric odometry.
Observation snapshots omit raw ultrasonic packets. Vision does not open the
sonar ray database or publish accumulated sonar points to the world map.
`robot.map.obstacles` reports `sonar_history_disabled` with empty ray and obstacle
lists. Live ultrasonic readings are available on the sensor stream.

The sonar mapper estimates local planar translation from changes in at least
three directionally independent, timestamped ranges and observed rotation.
Robust residual checks reject inconsistent surface matches; missing translation
evidence is unknown motion, never a stationary pose. This estimator assumes
locally static corresponding surfaces and a locally perpendicular beam/surface
model. Its uncertainty and planar assumptions remain visible. The odometry
state file holds the latest estimator state so it can recover after a restart.

Sonar ranges and these local poses are in metres. Live odometry operates without
a visual-scale attachment. LiDAR contributes geometry to the world map through
its existing fusion path.

The default mounts are measured from `03-1000017388.jpg` against the 20mm
checkerboard, combined with owner-supplied heights. `measure_rover_mounts.py`
retains the visible corner/landmark selections, ground homography and explicit
height-parallax model. `rover-mount-measurements.json` records the source image
hash and results: 1.48px grid reprojection RMS, and 101.52mm for the independent
printed 100mm ruler. These are image measurements with recorded uncertainty,
not substituted half-chassis-width guesses.

Camera-axis origins in millimetres (right, down, forward), rounded to 1mm
horizontally: front `[5,38.1,44]`, left `[-43,38.1,-49]`, right
`[43,38.1,-46]`, rear `[2,12.7,-152.35]`. Front/left/right acoustic centers are
25.4mm above the floor; rear is 50.8mm above it. The rear origin is computed from the photo-measured rear tire plane
(-146mm in camera forward coordinates) minus the owner-reported 6.35mm
protrusion, rather than leaving that dimension only in metadata.
Directions are the owner's forward, left, right and rear assignments. Camera
height is the previously supplied nominal 63.5mm. These defaults feed the actual
ray origins, camera beam projection and local metric sonar odometry. Explicit
`multisensor_mapping.sonar_mounts` configuration can override them.

This change contains no drive, steering, stop, LED, flashing or service-restart
commands. Installation must be scheduled outside another operator's experiment.
