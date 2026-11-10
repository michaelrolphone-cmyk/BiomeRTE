# Robot calibration and mapping preparation — 2026-11-10

Source reviewed: AHRS head 8dcad8391e0b83e8e5bc113195cf8ad5797c0f08,
stacked on radio/light PR50 and calibration-recovery PR49. This preparation
branch adds an independent read-only tool. It does not deploy or modify the
running controller, provider, camera, map, or robot.

## Findings from the code

| Area | Current implementation | Work for this robot |
| --- | --- | --- |
| Camera intrinsics | Retained calibration; image size and validation checked | Read actual retained calibration and use its stream dimensions; a moved board alone does not invalidate lens intrinsics |
| Metric scale | Exact-keyframe board capture, separate held-out validation, fit/apply, corrected-anchor resolution | New placement/session after moving the grid; do not mix prior board poses |
| Radio | Bounded adaptive fingerprints; native visual-relocalization candidates retain geometric checks | Install PR50 chain, enable polling, accumulate new independent visual labels, test position aliases |
| AHRS | Bounded per-sensor receipt history; resets and quaternion continuity retained | Install PR51 chain, enable polling, calibrate stationary gyro, qualify mounting/timing before pose correction |
| Light | Explicit authenticated command; frame brightness is reported | Inspect current image and light state, change brightness explicitly if needed, inspect subsequent images |
| Navigation geometry | Metric pose remains at camera optical center in native map axes; robot pose is null | Add qualified camera-to-robot and map-to-floor transforms |
| Traversability | Sparse points and semantic observations; occupancy/free space unavailable | Add observed obstacle/traversable/unknown representation, footprint and clearance |
| Motion | External brain; no relay control in vision service | Use rear-steering model and measured displacement, turning and stopping response |
| Timing | Snapshot age is query-time receive/decode age; exposure time is unqualified | Keep local perception/control loop on Mac, count response delay, measure source lag and actuator delay |
| Storage | Automatic checkpoint retirement and bounded sensor stores already implemented | Verify installed repair and disk budget; no per-frame full checkpointing |

Relevant implementations: vision_service.py (process_frame, _freshness, _robot_model,
query), metric_sessions.py (start_session, capture, status, metric_localization),
camera_source.py (_run), ahrs_tracking.py, radio_tracking.py, ground_projection.py.

The RF and AHRS adapters are observations and relocalization support. They do
not yet implement visual-inertial odometry, calibrated absolute heading or
inertial translation. Those are separate estimator work; the available data
must not be relabeled as completed fusion.

## Prepared preflight

Run with the installed vision Python (or a Python 3.10+ interpreter) on the Mac:

```sh
python3 providers/robot-vision/mapping_preflight.py --offline --grid-relocated
python3 providers/robot-vision/mapping_preflight.py --grid-relocated
```

Pass --state-dir if the service uses a different state directory.
The offline command reads config.json only. The online command reads metadata,
status and one snapshot from vision.sock; no image artifact is downloaded.
Output distinguishes configured sensor polling from actual runtime polling,
checks map/session/calibration identity, lens validation, pose transform,
receive/decode freshness including response time, image brightness and exact
metric-pose binding. No URLs or configuration credentials are emitted.

Exit 0 means the observed visual pipeline has the listed conditions for
mapping observations. Exit 2 means conditions are unmet or the read failed.
Neither outcome authorizes movement or declares a traversable map. The report
retains the current navigation-software gaps. A report is a point-in-time
observation; a control loop must reassess current evidence for every action.
The image-brightness check is an inspection trigger, not a visibility estimator.

Run the portable focused tests:

```sh
python3 -S -m unittest discover -s providers/robot-vision/tests -p test_mapping_preflight.py -v
```

The dedicated workflow runs these tests on Linux and macOS. They validate the
preflight's interpretation and command boundary, not native SLAM or hardware.

## Resume sequence after the controller upgrade

1. Confirm the actual installed provider source before changing it. The PR51
   head contains the reviewed PR50/49/48/47/46 chain; preserve retained camera
   calibration, maps, state and robot-model configuration. Do not restart or
   replace a provider still owned by the upgrade worker.
2. Enable radio.enabled and ahrs.enabled in the existing camera configuration,
   preserving URL/authentication and unrelated configuration. Initial reviewed
   intervals are 5 seconds for radio and 0.1 seconds for AHRS; measure actual
   camera/stream behavior before increasing load. Run the preflight and inspect
   coherent raw images plus each sensor's validity, age and reset identity.
3. Inspect light state and current images. If dark, send one explicit light
   adjustment and check a later frame for actual visibility and glare.
   Do not retry an uncertain mutation as a new command.
4. With the robot stationary, explicitly request gyro calibration and observe
   its later state. A queued response is not completion; the current firmware
   has no request identifier for conclusive request/result correlation.
5. Preserve lens calibration. For the relocated checkerboard, cancel any old
   collecting/ready/provisional scale session before starting a new one. The
   board is 9 by 6 inner corners; nominal square spacing is 20 mm. Physical
   spacing is not verified by the nominal printed design. Record actual
   measurement provenance before applying metric scale.
6. Recover localization against stable room features or initialize a fresh
   map while preserving the old one. Use translated views with the board fixed
   for the entire new scale session. Current board visibility alone does not
   make an observation an exact native keyframe. Inspect accepted training and
   validation captures, fit reasons and attachment status.
7. Fit and verify camera-to-robot, IMU-to-camera and floor-frame geometry as
   separate attachments. The grid's placement must be declared (floor/wall/
   other); its presence in front of the robot does not imply a floor plane.
   Rigid extrinsics use a documented transform direction and provenance.
8. Measure motion against qualified camera/grid poses and AHRS rotation.
   Preserve confirmed rear steering, relay direction mapping and 250 ms
   steering lead. Fit forward/reverse displacement vs duration, moving steering
   response, turning radius, delay and coast/stop behavior with held-out trials.
   Stationary steering does not imply immediate maximum wheel angle.
9. Build and retain a map with verified stable landmarks, explicit floor/robot
   transforms and separately observed traversable/obstacle/unknown cells.
   Do not rasterize missing sparse landmarks as clear floor.
10. Qualify a short waypoint route and return: compare intended vs observed
    displacement/heading, test stop on lost localization/stale observations,
    and demonstrate fresh relocalization after reopening the saved map.

## Remaining implementation order

1. Versioned rigid transform attachments and map-revision-aware robot/floor pose
   derivation, including loop-correction and map/session reset behavior.
2. Evidence-backed local traversability layer with bounded retention,
   footprint inflation and revision-aware reads; unknown remains unknown.
3. Rear-steering response fitting and a same-Mac feedback brain using the Unix
   vision API and actual relay capability path. It must distinguish command
   acceptance, physical actuation and measured displacement.
4. Bounded exploration/waypoint execution with fresh evidence, explicit
   interruption/stop and saved-map relocalization acceptance.

AHRS-assisted pose estimation can follow mounting/timing qualification.
Pressure altitude is not floor clearance; acceleration includes gravity.
No North-referenced compass or RF metric position is assumed.

## Acceptance evidence

A successful sparse map requires retained real keyframes/landmarks and
fresh relocalization after reopening it. A successful metric map additionally
requires a qualified independent scale attachment and held-out error report.
A navigable map additionally needs qualified floor/robot geometry and observed
clearance, followed by an actual feedback-controlled route and return.
Record source/config identities, frame/snapshot IDs, map revisions, scale/
transform attachment IDs and observed error measurements with those outcomes.
