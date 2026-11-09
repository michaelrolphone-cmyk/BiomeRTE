# Mac robot vision provider

This provider continuously decodes the ESP32 camera on the Mac, runs YOLO11n with
BoT-SORT, and maintains a durable observation store and a reusable monocular SLAM
map. The driving brain is a separate consumer. This provider does not operate
motors or relays.

## Current camera configuration

The verified source is RTSP over TCP at
rtsp://192.168.1.45:554/mjpeg/1, with 1280 x 720 MJPEG images at approximately 5 fps.
The firmware also exposes HTTP /stream and /snapshot, but the continuous service
uses RTSP.

The supplied mounting height is 0.0635 m (2.5 inches), provisionally measured at
the optical center. "Ninety degrees to the ground" is interpreted as a vertical
camera face and a horizontal optical axis: pitch_down_deg = 0. Roll is assumed
level and remains unmeasured. These inputs are recorded with provenance.

Intrinsics and distortion are initially **unmeasured**. Capture, detection,
tracking, observation persistence and the API work in that state. The SLAM
backend reports awaiting_calibration, and geometric positions remain unavailable.
An unknown pose is never an identity matrix or a zero-position estimate.

## Runtime layout

- STATE defaults to ~/.local/share/biome-robot-vision.
- STATE/env is a dedicated native Apple Silicon environment.
- STATE/config.json holds non-secret camera/model/calibration settings.
- STATE/observations holds SQLite state and immutable rolling image blobs.
- STATE/maps holds backend saves.
- STATE/runtime.json selects the active map and calibration.
- STATE/vision.sock is the owner-only local API.
- The provider uses its own identity, worker ledger and LaunchAgent.
- The existing installed Mac terminal gateway supplies authenticated BiomeRTE
  transport. Its credentials are not copied into this repository.

## Setup from the repository root

Run these commands from the repository root, using the existing native
Miniconda Python only to bootstrap the dedicated environment. All later phases
use that environment. The SLAM setup takes `--state-dir`; detector setup takes
`--state`.

```sh
VISION_STATE="$HOME/.local/share/biome-robot-vision"
VISION_BOOTSTRAP_PYTHON="$HOME/miniconda3/bin/python3"

"$VISION_BOOTSTRAP_PYTHON" providers/robot-vision/setup_slam.py \
  --state-dir "$VISION_STATE" --phase environment
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_detector.py \
  --state "$VISION_STATE"

"$VISION_STATE/env/bin/python" providers/robot-vision/setup_slam.py \
  --state-dir "$VISION_STATE" --phase source
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_slam.py \
  --state-dir "$VISION_STATE" --phase patches
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_slam.py \
  --state-dir "$VISION_STATE" --phase native --jobs 4
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_slam.py \
  --state-dir "$VISION_STATE" --phase python
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_slam.py \
  --state-dir "$VISION_STATE" --phase vocabulary
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_slam.py \
  --state-dir "$VISION_STATE" --phase smoke
```

`--jobs 4` is optional native-build parallelism. Complete each phase
successfully before continuing. Detector installation preserves conda's
NumPy/OpenCV. The setup shares one OpenMP runtime between PyTorch and native
OpenCV, verifies both import orders, and records the original library hash.
It does not set `KMP_DUPLICATE_LIB_OK` or modify the base Python environment.

Run install_provider.py without install flags to inspect its plan. Install and
start the two user services with:

```sh
"$VISION_STATE/env/bin/python" providers/robot-vision/install_provider.py \
  --state "$VISION_STATE" --install --start
```

The default service mode is the existing Python LaunchAgent. The perception
service and capability worker have separate lifetimes and logs. Neither
requires a Raspberry Pi. An explicitly selected service mode is preserved on
reinstallation.

### Optional named macOS application

macOS can apply different Local Network privileges to a terminal-launched
command and a LaunchAgent. Apple explains the distinction in
[TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy).
An errno 65 network failure alone does not prove a privacy denial.

For a recognizable application identity and normal Local Network consent, the
optional native wrapper is **Biome Robot Vision.app**. Its native process stays
alive and owns the Python child. It requests ordinary user-level access; it
does not edit privacy databases, run as root, or change system network policy.
A parent TCP probe reports only that parent's connection state, separately from
the Python child's camera/decode health.

Prepare and inspect the app while the current services continue running:

```sh
VISION_APP_BUILD="$VISION_STATE/build/macos-app/$(uuidgen)"
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_macos_app.py \
  --state-dir "$VISION_STATE" --prepare --output-dir "$VISION_APP_BUILD"
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_macos_app.py \
  --verify-prepared "$VISION_APP_BUILD"
```

Preparation compiles and verifies a staged bundle, configuration and launch
specification. It does not install the app, start it, or request Local Network
permission. Signing provenance is recorded. Apple recommends an Apple-issued
signing identity for reliable permission identity tracking; a development
ad-hoc build does not establish persistence of the grant across rebuilt versions.

To explicitly switch to the prepared app, first stop the existing services and
allow their graceful cleanup to finish. The installer refuses a loaded service
or held state lock rather than starting a second owner:

```sh
"$VISION_STATE/env/bin/python" providers/robot-vision/install_provider.py --stop
"$VISION_STATE/env/bin/python" providers/robot-vision/setup_macos_app.py \
  --state-dir "$VISION_STATE" --install-prepared "$VISION_APP_BUILD"
"$VISION_STATE/env/bin/python" providers/robot-vision/install_provider.py \
  --state "$VISION_STATE" --service-mode macos-app --install --start
```

The app is installed in ~/Applications/Biome Robot Vision.app. If macOS requests
Local Network access, the user grants it to that named app in System Settings
under Privacy & Security > Local Network. Actual RTSP decoding through the
Python child is the acceptance check; a successful parent probe is insufficient.

An intentional Quit stops the app cleanly; launchd restarts unexpected app
failures. The wrapper waits for its child to finish checkpointing without a
forced shutdown deadline. If the app disappears unexpectedly, the child detects
reparenting and follows the same graceful shutdown path. The app and service
hold separate per-state locks until their owned work is finished.

The Python LaunchAgent remains the default; preparation does not select the app.
Use --service-mode python on a later installation to explicitly select direct
Python again.

## Capabilities

All names below have version :1. The BiomeRTE adapter wraps local responses under
result. Inputs are optional unless specified.

| Capability | Inputs | Result |
| --- | --- | --- |
| robot.vision.snapshot | snapshot_id, include_frame=false | Coherent image metadata, detections, tracks, localization, geometry and map revision; optional exact image artifact |
| robot.vision.frame | snapshot_id or frame_id | Exact retained image and its checksum/artifact manifest |
| robot.vision.detections | snapshot_id | Image-space detections and session-scoped tracking IDs |
| robot.vision.geometry | snapshot_id | Localization and spatial evidence, with availability/quality/provenance |
| robot.vision.metadata | none | Calibration, axes, clocks, supported layers, assumptions and retention |
| robot.vision.status | none | Camera, inference, localization, persistence and capability health |
| robot.vision.calibration | action defaults to get; calibration_id optional for get; set requires calibration object | Read the active or specified preserved calibration, or preserve the old map and begin a new calibrated map |
| robot.vision.project_ground | pixel=[u,v], confirmed_floor_contact=true; snapshot_id | Calibrated ray/plane intersection for a caller-confirmed contact pixel |
| robot.map.read | map_id, revision, bounds, kinds, cursor, limit | Map entities at one exact revision; paginated region/layer reads |
| robot.map.changes | since_revision required; map_id, to_revision, cursor, limit | Revision-pinned changes including deletion tombstones |
| robot.map.checkpoint | map_id (active map) | SQLite snapshot and complete backend package with checksums |
| robot.map.create | metadata | Create a new map without selecting it |
| robot.map.open | map_id required | Select a preserved map; create a new observation session and relocalize |

Bounds use min_x, min_y, max_x, max_y and optional min_z, max_z. Map coordinates
remain arbitrary monocular units until an independently qualified scale estimate
exists. A metric ground projection does not silently rescale the SLAM map.

Prefer snapshot for control decisions. Keep its snapshot_id when requesting the
corresponding image, detections or geometry. A separate "latest" request may
refer to a newer observation. Expired image requests explicitly report
unavailable; the API never substitutes a later image under an old identifier.

`snapshot.detections` is a list of detection items. Per-frame detector envelope
metadata is stored in `snapshot.frame.detection_metadata`. Consumers should
check that metadata and the localization/geometry availability fields before
interpreting an empty detection list or using a pose.

For `robot.vision.calibration`, `{}` reads the active calibration because
`action` defaults to `get`. Supply `calibration_id` to retrieve a preserved
calibration record; explicit `action: "get"` is also supported. Setting a
calibration uses `action: "set"` with a `calibration` object.

The current-frame freshness field measures time since Mac receive/decode,
because this camera stream has no synchronized capture timestamp. RTP media
timestamps are exposed as relative media time, not UTC. The receiver keeps only
the latest undecided frame so a slow consumer skips frames rather than building
an ever-growing processing backlog.

Freshness is assessed when the local service answers the query. Its
`assessed_at_mac_utc` and `valid_until_mac_utc` record that assessment and its
expiry; `delivery_latency_included` is `false`. The independent brain must
recheck age/expiry when the response arrives and before acting, since delivery
can outlast the freshness window. A remote consumer also needs to account for
clock offset and uncertainty when comparing Mac timestamps with its own clock.
These timestamps do not measure camera exposure age or subsequent response
delivery latency.

## Map semantics and persistence

The durable map consists of keyframes, sparse points and semantic observations.
A semantic observation is anchored only when the backend verifies that the
current input actually became that keyframe. It does not promote a moving-camera
tracker ID into a permanent object identity.

Backend corrections update the same map entities in a new revision. Historical
revisions remain queryable. An observation retains the revision against which its
pose was calculated. A culled anchor remains historical evidence and cannot
supply a current global position without explicit reanchoring.

A backend checkpoint includes the actual feature/landmark/keyframe and
place-recognition state used by the SLAM implementation. An occupancy image, a
point cloud, or a SQLite copy alone is not treated as a relocalizable checkpoint.
Restart keeps map identity but creates a new session and requires fresh visual
relocalization. It never assumes that the camera is still at the last saved
pose. Reconciliation removes geometry newer than the recovered checkpoint from
the current view while retaining historical revisions.

Rolling observations are limited by max_observations (default 300). Keyframe
metadata and pinned keyframe images are durable. Full video is not journaled to
BiomeRTE: requested images are uploaded on demand in checksum-verified chunks.

## Geometry and navigation interpretation

- Camera optical axes are x right, y down, z forward. Camera poses refer to the
  optical center; robot-center extrinsics are initially unavailable.
- Ground-projection axes are x forward, y left, z up, with origin on the ground
  directly below the camera.
- Positive pitch angles point the optical axis downward.
- A bounding-box bottom is only a **candidate** ground-contact pixel. It is
  never automatically converted to a claimed object distance.
- Ground projection requires validated intrinsics, distortion, matching image
  dimensions, supplied mounting geometry and an explicit contact assertion.
- The result includes ray sensitivity and provenance. Missing uncertainty
  inputs produce null covariance, not a fabricated precision estimate.
- Sparse points and absent detections do not establish free space. Occupancy,
  traversability, drop-offs, metric depth and metric robot velocity are explicitly
  unavailable until supported estimators or sensors are integrated.
- Image-space object motion includes camera motion. It is not world velocity.
- Loop-closure corrections are not differentiated into vehicle speed.

These fields let a separate brain reason from the evidence actually available
and identify missing localization, scale or obstacle information.

## Camera calibration

Collect images of a flat checkerboard at the full stream resolution, with the
board at varied positions, distances and tilts across the image. Keep focus,
lens correction and image resolution unchanged between calibration and use.
Specify **inner-corner counts** and a measured square size.

For a board with 9 by 6 inner corners and 20 mm squares, run this example from
the repository root with `VISION_STATE` set as above:

```sh
"$VISION_STATE/env/bin/python" providers/robot-vision/calibrate_camera.py \
  --images /absolute/path/to/checkerboard-images \
  --columns 9 --rows 6 --square-size-m 0.020 \
  --height-m 0.0635 --pitch-down-deg 0 \
  --output /absolute/path/to/camera-calibration.json
```

The utility records reprojection error, per-view errors and input checksums.
Submit the resulting object to robot.vision.calibration with action=set.
Changing calibration preserves the old map and creates a new active map.
Intrinsic fitting does not by itself qualify navigation, ground contact,
mounting accuracy, or monocular scale.

## Local API and validation

The local API accepts `POST /v1/query` over `$VISION_STATE/vision.sock`. An
independent brain on the same Mac should use this owner-only Unix HTTP API for
its perception loop; it avoids a BiomeRTE controller round trip for every
frame. BiomeRTE remains available to remote consumers and for on-demand queries.

```sh
curl --unix-socket "$VISION_STATE/vision.sock" \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"operation":"robot.vision.snapshot","inputs":{"include_frame":false}}' \
  http://localhost/v1/query
```

The operation name in a local request omits the BiomeRTE contract's `:1`
suffix. Use `include_frame: true` when the consumer needs the exact image
attached to that snapshot, and preserve its IDs across subsequent requests.

It returns direct JSON. A supported but unavailable measurement is an HTTP 200
response with an explicit reason. Malformed/unknown requests receive an error.
The BiomeRTE adapter preserves those errors inside its completed result so an
ordinary query error does not strand a capability lease.

Run the focused tests from `providers/robot-vision` with the dedicated
environment:

```sh
"$VISION_STATE/env/bin/python" -m unittest discover -s tests -v
```

The tests exercise exact observation identity, retention, revision conflicts,
historical geometry, restart/relocalization state, ground projection signs and
invalid-calibration behavior, and the provider/installer boundaries. Genuine
SLAM save/load/relocalization requires the additional recorded-sequence
qualification; a fake-backend test does not establish native SLAM performance.

## Recorded-sequence native qualification

The selected native Apple Silicon ORB2/g2o/DBoW3 profile passed a real
save/load/relocalization qualification on 2026-11-09. Eleven recorded KITTI06
inputs produced nine valid poses. The saved state contained six keyframes,
471 sparse points, 16,000 keypoints, descriptor records and its DBoW3 database.
A fresh engine loaded that state with no valid current pose, then successfully
relocalized a non-keyframe image against the saved map. Both engines shut down
cleanly. The recorded result, input/source hashes and scope are in
[validation/native-kitti06-20261109.json](validation/native-kitti06-20261109.json).

This is evidence for this engine profile and sequence, not a live-camera
navigation qualification. Native initialization and full map restore can take
tens of seconds; status and query completion must be interpreted accordingly.

After setup succeeds, run the recorded-sequence qualifier from the repository
root. Use a fresh run directory so its evidence belongs to one execution:

```sh
VISION_QUALIFICATION_RUN_ID="$(uuidgen)"
"$VISION_STATE/env/bin/python" providers/robot-vision/tests/qualify_slam.py \
  --state-dir "$VISION_STATE" \
  --output "$VISION_STATE/qualification/kitti06/$VISION_QUALIFICATION_RUN_ID"
```

The run directory contains `qualification.json`, `trajectory.json`,
`relocalization_input.png`, and the saved backend package in `system_state/`.
Inspect the qualification outcome and its recorded evidence before reporting
native success. Qualification on this recorded sequence does not calibrate the
RC camera or establish metric scale, obstacle coverage, or safe driving on the
car. The camera's intrinsics and distortion remain unknown until measured;
the supplied 0.0635 m height and provisional zero downward pitch do not replace
that calibration.
