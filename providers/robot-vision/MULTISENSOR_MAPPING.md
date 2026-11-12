# Camera, AHRS and four-sonar mapping

The service uses capture-clock attitude increments to seed pySLAM's rotational
motion prediction, then combines the visual orientation with the AHRS prediction.
Visual translation is combined with a separate local metric sonar odometry track. A lost visual pose does not become a
stationary pose just because orientation is available. Large visual orientation
innovations re-anchor the filter, preserving relocalization and loop closure.

`multisensor_mapping.ahrs.R_camera_imu` supplies a measured IMU-to-camera rotation.
Without it, paired visual and AHRS rotation increments estimate a mount passively.
Predominantly planar motion constrains only its observed rotation axis; this is
an estimated mount, not a claim of full six-degree calibration.

Firmware adds four-sonar history to `biome.capture.v1`. Each transducer retains
its own transmit/echo midpoint measurement time and completion time, on esp_timer.
The capture packet selects only returns both measured and completed before the
image capture. Missing history stays unavailable. Separate latest HTTP telemetry
and the independent latest-image live feed keep their existing behavior.

The host interpolates camera poses in that same boot's capture clock. Each
accepted sonar observation is stored once in `sonar-obstacles.sqlite3`, along
with range in metres, mount geometry, sample identity, source frame, local pose,
and relative keyframe anchor. Missing poses retain raw observations rather than fabricating points. Sonar
poses and ranges form a local metric map even without visual scale. A later bracketing pose can resolve
pending returns. Different local maps retain separate observations. Reading
reprojects anchors at the requested map revision, so corrected geometry moves
previous returns consistently. Map checkpoints do not clone the ray ledger.

`robot.map.obstacles` exposes accumulated spatial rays, occupied endpoint voxels,
centerline clearing evidence, sample counts and beam uncertainty. World-map reads
include sonar endpoints as orange sparse points, in the existing pagination and
submap transforms. Unaligned local submaps are retained and stay out of world
coordinates until the atlas establishes their transform.

The sonar mapper estimates local planar translation from changes in at least
three directionally independent, timestamped ranges and observed rotation.
Robust residual checks reject inconsistent surface matches; missing translation
evidence is unknown motion, never a stationary pose. This estimator assumes
locally static corresponding surfaces and a locally perpendicular beam/surface
model. Its uncertainty and planar assumptions remain visible. Local metric pose
and map identities persist across restarts; measurement gaps create separate
retained local frames rather than stacking observations at a reused origin.

Sonar ranges and these local poses are already in metres. No visual-scale
attachment is required for their accumulation. Timestamp-matched visual and
sonar trajectories estimate the transformation into each native visual submap.
Straight driving can estimate scale because paired camera orientations constrain
the axes. This is labelled an estimated sonar/visual alignment, and is recomputed
from current keyframe anchors after map corrections. Existing qualified metric
attachments can also place sonar rays in the visual map. Unaligned metric
submaps stay available through the obstacle query until world placement is known.

Sonar
endpoints are beam centerline evidence, not exact wall samples; the HC-SR04 beam
width and mount uncertainty remain attached. Free-space evidence along a ray
does not certify vehicle clearance or traversability.

Configure `multisensor_mapping.sonar_mounts` by sensor ID with
`origin_camera_m`, `direction_camera`, and `beam_half_angle_deg`. Camera axes are
x right, y down, z forward. Current fallback mounts use the user-reported chassis
dimensions (5.5in wide, 7.25in long, camera 1.75in behind front). These estimates
are explicitly labelled `chassis_estimate`; the front sonar uses the user-reported 25.4mm height above the floor and
38.1mm offset below the nominal camera optical center, pointing forward along
the camera axis. The host projects its beam region into the calibrated image;
it does not assume an exact center pixel. Other sensor height offsets and exact
mount positions still require measurement. They must not be described as calibrated.

This change contains no drive, steering, stop, LED, flashing or service-restart
commands. Installation must be scheduled outside another operator's experiment.
