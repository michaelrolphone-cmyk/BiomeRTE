# Run multiple robots

Each robot has an instance ID, a body profile, and its own vision state directory.
The current Rover defaults to ID and profile `rover`. Its relay contract and
provider resource keep their existing addresses. Each additional robot gets a
separate resource, stream lease, calibration, atlas, SLAM state and LiDAR store.
Clients select a robot by its provider resource and can read
`robot.body.model:1` and `robot.control.model:1` on that resource.

## LAFVIN quadruped

The [body model](spider-body-model.json) uses metres and body axes forward,
left and up, with the origin at the chassis center.

| Quantity | Value | Source |
| --- | --- | --- |
| Catalog envelope | 170 × 160 × 90 mm | Catalog dimensions. The pose and axis assignment are unspecified. |
| Legs and actuation | 4 legs, 2 MG90S servos per leg | LAFVIN assembly and factory source. |
| Standoffs | 25 mm | LAFVIN assembly hardware. |
| Body plate | Approximately 112 × 83 mm | Overhead assembly drawing scaled by the nominal MG90S flange span. |
| Hip spacing | Approximately 85 mm fore–aft and 71 mm lateral | Same overhead drawing. |
| Side-view projections | Approximately 32 mm hip to lift joint and 38 mm lift joint to foot | Side drawing scaled by the nominal MG90S case length. |

The model links the exact assembly revision, views and scale references. The drawing
estimates support visualization. The vendor files omit calibrated joint axes, full
3D offsets, servo zero offsets and the positions of the add-on camera, LiDARs and
BNO085. Save measured geometry as a higher revision of the model in
`STATE/robot-model.json`, retaining the `lafvin-spider` profile. This metadata
and the firmware's leg calibration have separate consumers. Use the
[firmware calibration procedure](https://github.com/michaelrolphone-cmyk/BiomeRTE/pull/262)
to qualify servo-based foot positions and displacement.

## Bind a spider instance

1. Create a new state directory and put this `config.json` in it. Use the
   receiver ID configured in the ESP32-S3 firmware.

   ```json
   {
     "robot": {"id": "spider", "model": "lafvin-spider"},
     "camera": {"source_id": "spider-camera", "capture_transport": "websocket_push"}
   }
   ```

2. In the same directory, set up the private `sensor-push.json` with that
   `receiver_id`, a LAN-facing `listen` address, an unused `port` and a
   dedicated `token` matching the firmware. Point the firmware at this
   listener's `/camera` and `/sensors` paths. To enable commands, set
   `motion_enabled` to true and set a separate `motion_secret` matching the
   firmware (at least 32 bytes). The motion route is otherwise disabled.
3. Set up `STATE/env` and the native SLAM dependencies as in the
   [vision setup](README.md). Shared pySLAM source can be specified as the
   absolute `slam.pyslam_root` path. On the Mac, give this instance's provider
   identity to the existing gateway configuration and start it with
   `python providers/robot-vision/install_provider.py --state STATE --install --start`.
   The installer prints the resource, which ends in `.spider` for this example.
   For later robots, use a different instance ID, state directory, receiver ID,
   port and token. Each new model can be supplied in its own `robot-model.json`
   with the corresponding profile.
4. Qualify the camera intrinsics for the actual image resolution. Use
   `robot.vision.rig.configure:1` or write `STATE/sensor-rig.json` with schema
   `rover.sensor-rig/1`, the instance's `robot_id`, a positive `revision`, and
   a `lidar` dictionary keyed by `spider-tof-0` and `spider-tof-1`. For each
   measured mount, set its `camera_id` to `front`, since both sensors report
   through the same ESP32-S3. Set `projection_camera_id` to `front` for the
   forward-facing grid and to `null` for the rearward grid with the
   single-camera configuration. The rearward grid still contributes to the
   LiDAR map when it has a qualified pose. If a rear camera is added and
   calibrated, set `projection_camera_id` to `rear` for the rearward grid.
   Set `validated`, `T_front_camera_sensor_m`, horizontal and vertical field of view,
   `flip_x`, `flip_y`, `valid_statuses` and `distance_semantics`. Each
   transform maps the sensor's right/down/forward optical axes into the front
   camera's optical axes. For body-frame LiDAR poses, also supply the measured
   `camera.T_robot_camera` in the body model. Raw grids remain visible before
   calibration. The body model assigns `spider-tof-0` to forward and
   `spider-tof-1` to rearward. Check the multiplexer wiring against those IDs
   when calibrating. Each grid can supply depth to its matching optical view
   and contribute points behind or ahead of the robot to the same map.

## Send a walk or pose

Use `robot.motion.command:1` on the spider's provider resource. A walk request
uses `{"robot_id":"spider","mode":"walk","action":"forward"}`. Query
`robot.control.model:1` for additional gait names, named poses and joint limits.
A custom pose uses `mode: "joint_pose"`, `action: "custom"`, and the fields
`angles_deg`, `duration_ms` and optionally `stance_mask`. The array follows
the advertised `gpio_order`. For origin zeroing use `mode: "origin"` and
`action: "zero"`. A stop uses `mode: "stop"` and `action: "none"`.

Read `robot.motion.status:1` or the live `spider_motion` telemetry for accepted
and acknowledged sequences, joint targets and servo odometry. The command reply
reports queue acceptance. Servo odometry and raw `bno085` readings retain
their own frames and qualification. Applications can compare them with the
spider's visual and LiDAR map without treating servo targets as measured angles
or BNO085 game rotation as an absolute compass heading.
