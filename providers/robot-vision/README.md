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

On a fresh installation, intrinsics and distortion are **unmeasured**. Capture,
detection, tracking, observation persistence and the API work in that state.
The SLAM backend reports awaiting_calibration, and geometric positions remain
unavailable. Read robot.vision.calibration and robot.vision.status for the active
installation: a validated calibration can already be preserved and applied.
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

All 21 names below have version :1. The original 13 envelopes are unchanged;
the eight scale and metric-read contracts are additive. The BiomeRTE adapter
wraps local responses under result. Inputs are optional unless specified.
The worker advertises an operation as ready only when the running service lists
it as supported.

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
| robot.map.scale.start | method required: checkerboard or measured_distance; reference, policy, auto_capture, auto_apply | Persistent scale session; checkerboard reference is required for that method |
| robot.map.scale.status | session_id, map_id, include_observations=false | Session state, retained evidence, fit qualification and exact-keyframe capture eligibility |
| robot.map.scale.capture | session_id and snapshot_id required; role=training or validation; label=start, end, validation_start or validation_end | Evidence from the exact native keyframe represented by that snapshot |
| robot.map.scale.fit | session_id required; distance_m, validation_distance_m | Candidate scale and independent validation; failed fits keep the session collecting |
| robot.map.scale.apply | session_id required | Apply a qualified candidate and return its metric attachment |
| robot.map.scale.cancel | session_id required | Cancel scale collection and return the persisted session state |
| robot.map.scale.reference | session_id and reference required | Replace an unapplied board reference and recompute preserved board evidence |
| robot.map.metric.read | map_id, revision, attachment_id, bounds, kinds, cursor (object), limit | Qualified metric view at a pinned raw-map revision; explicit unavailable/stale state |

Bounds use min_x, min_y, max_x, max_y and optional min_z, max_z. Map coordinates
remain arbitrary monocular units until an independently qualified scale estimate
exists. A metric ground projection does not silently rescale the SLAM map. Bounds passed
to robot.map.metric.read are metres; bounds passed to robot.map.read retain raw
map units.

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

### Checkpoint retention

Automatic and recovery saves retain the newest verified database backup and
two complete backend generations per preserved map: current and rollback.
Explicit `robot.map.checkpoint` saves remain retained. Older unclassified
checkpoints are preserved unless a deliberate legacy audit retires them using
the same integrity and replacement checks.

Backend staging is temporary. A checkpoint becomes available only after its
files, database backup and manifest are complete. Superseded automatic copies
are marked retired before their files are removed; a retired checkpoint reports
unavailable with reason `checkpoint_retired`. Retention does not delete maps,
calibration records or historical map revisions. Complete orphan publications
are adopted only when their map revision also exists in the live database.

`robot.vision.status` exposes `checkpoint_error`,
`checkpoint_cleanup_error`, and `persistence.checkpoint_storage` independently
of frame-processing health. Automatic retry follows `checkpoint_interval_s`,
measured after every attempt, including failures. A failed save does not retry
on every camera frame.

Headless checkpoints omit the optional `kf_image_map.pkl` visualization cache.
They retain the real map, descriptors and place-recognition database. Existing
packages remain integrity checked, and restore still requires fresh visual
relocalization before publishing a valid current pose.

### Robot configuration metadata

An optional `robot-model.json` in the state directory holds the robot's
versioned configuration and its measurement provenance. It is independent of
lens calibration and does not switch maps. `robot.vision.metadata.robot_model`
returns the full definition; `robot.vision.status.robot_model` returns its
identity, revision and qualification status. Replace the file atomically to
publish a configuration update without restarting perception.

Reported mechanics and candidate relay directions remain distinct from
measured dimensions and validated motion predictions. This metadata does not
drive relays, create odometry, qualify metric scale, or supply a camera-to-body
transform. Those measurements retain their existing explicit availability.

Rolling observations are limited by max_observations (default 300). Keyframe
metadata and pinned keyframe images are durable. Full video is not journaled to
BiomeRTE: requested images are uploaded on demand in checksum-verified chunks.


## Metric scale sessions

A calibrated monocular camera still needs an independent physical reference
to attach metres to its SLAM map. The scale service keeps raw SLAM coordinates
unchanged and stores a qualified metric attachment separately. A metric view
multiplies translations, point coordinates and bounds by that scale. It retains
the map's origin and axes; it does not establish a floor frame, gravity, robot
center, occupancy or free space.

Snapshots, geometry and status expose additive metric_localization and
metric_scale telemetry. The original localization and raw map fields remain
in arbitrary monocular units. Require explicit metric availability and fit
qualification before using metres. A missing or provisional scale is not a
zero distance or a ready metric pose.

Every scale operation is observational or computational. None drives the car,
operates relays, requires a Raspberry Pi, or treats commanded motion as a
measurement. An independent driving brain or human supplies camera movement.
The camera must translate enough for genuine native SLAM initialization and
usable scale evidence; rotating a stationary camera does not supply that
translation.

### Stationary checkerboard method

Use the calibrated camera at its calibrated image dimensions. Put a rigid, flat
checkerboard on the floor or against a wall and keep it stationary for the
entire session while the camera moves through different views. Its placement
does not establish map alignment to that surface. A 9 by 6 **inner-corner** board
has 10 by 7 squares. Measure physical square spacing on the actual board and
record how it was measured; printer settings and nominal design dimensions
alone do not verify physical size.

Start with method=checkerboard and a reference object. inner_corners may be
omitted for the [9,6] default. The supported pattern is rectangular, with each
inner-corner count at least 3. square_size_m must be a positive number,
size_verified records whether the physical size was measured, measurement_source
records its provenance, and stationary must be true. reference_board_id is an
optional caller identifier. auto_capture defaults true for this method.
auto_apply defaults false.

This local API example intentionally uses an **unverified nominal** 20 mm design
so it cannot claim verified metric scale. Replace the board geometry and
provenance with the actual reference being observed:

```json
{
  "operation": "robot.map.scale.start",
  "inputs": {
    "method": "checkerboard",
    "reference": {
      "inner_corners": [9, 6],
      "square_size_m": 0.020,
      "size_verified": false,
      "measurement_source": "nominal printed design; physical spacing not measured",
      "stationary": true,
      "reference_board_id": "stationary-floor-board"
    },
    "auto_capture": true,
    "auto_apply": false
  }
}
```

Keep the returned session_id. Read robot.map.scale.status with that ID and,
when needed, include_observations=true. Automatic collection retains qualifying
exact native-keyframe observations and assigns independent held-out evidence
deterministically. Current board visibility is assessed separately: detecting
corners in an ordinary frame does not make it an eligible keyframe measurement.
Follow the reported eligibility and fit reasons rather than assuming a number
of seconds or a number of delivered frames proves qualification.

Call robot.map.scale.fit with session_id after useful evidence is collected.
The service checks geometric consistency and independent validation. A fit
that does not qualify leaves the session collecting so more evidence can be
added. A geometrically useful fit with unverified physical size puts the session in
provisional. Inspect session.state together with candidate qualified and
failed_checks: the fitter can also use its own status=provisional for incomplete
evidence. Only failed_checks=["reference_size_unverified"] identifies the
size-only qualification gap. A qualified candidate can become ready. Call robot.map.scale.apply explicitly
to attach it; auto_apply=true permits automatic application only when those
same qualification checks succeed.

To upgrade nominal dimensions after measuring the board, call
robot.map.scale.reference with session_id and the **complete corrected reference
object**. Set size_verified=true only for a verified physical measurement and
record measurement_source. The service rebuilds board poses from its preserved
raw image corners and invalidates the old candidate, so the images need not be
recollected solely to correct square size. Fit again before applying. Reference
replacement is permitted only before application; replacing an applied
reference requires a new session.

| Session state | Meaning for a consumer |
| --- | --- |
| collecting | Retaining evidence; a failed fit can return to or remain in this state |
| ready | Candidate qualified for explicit application |
| provisional | Useful fit without a verified physical reference; do not use it as applied metric scale |
| applied | Qualified scale attachment was applied; still check current attachment validity |
| canceled | This collection session has ended |

### Manual capture and measured-distance fallback

Manual robot.map.scale.capture requires session_id and snapshot_id. The snapshot
must correspond exactly to a native keyframe with the same source_frame_id,
in the applicable map/session. There is no pose interpolation between keyframes.
Use scale status to find the last eligible exact-keyframe snapshot and the
reason a current frame is ineligible. Retain the evidence associated with that
specific image, pose and map revision.

For method=measured_distance, measure the Euclidean displacement between the
**camera optical-center positions represented by the two captured endpoint
snapshots**. This is the straight-line separation of those positions, not
distance integrated along a curved path, wheel travel, a robot-center estimate
or a commanded distance. If the camera has moved away from the pose represented
by a selected snapshot, a new physical measurement cannot silently be assigned
to that historical endpoint.

A measured-distance session needs a training pair and a separate independently
measured validation pair using entirely different native keyframe endpoints:

| Endpoint capture | role | label |
| --- | --- | --- |
| Training start | training | start |
| Training end | training | end |
| Validation start | validation | validation_start |
| Validation end | validation | validation_end |

Start with method=measured_distance, then capture each exact endpoint snapshot
using its corresponding label. Pass both distance_m and validation_distance_m
as positive JSON numbers to robot.map.scale.fit. Set distance_m to 3.0 only
when the actual training endpoint separation was measured as 3 m. Record the
separate validation displacement; reusing the fitted pair as its own validation
does not establish an independent check. The service never generates the
required motion or infers either measurement from a relay command.

### Metric reads, revisions and loop corrections

Use robot.map.metric.read for paginated metric geometry. Pin the returned map,
raw-map revision and metric attachment identifier when comparing pages.
Pass the returned next_cursor as the next request's cursor JSON object without
stringifying it. It contains the pinned query and native_cursor; carry both
unchanged. Its bounds are metres. robot.map.read and robot.map.changes retain
their existing raw coordinate semantics.

Scale evidence retains the board measurements and exact keyframe pose anchors.
If a loop closure or other actual anchor correction changes those poses, the
service refits against the corrected evidence. An unresolved correction makes
the metric result explicitly stale or unavailable; the prior scale must not
silently be treated as qualified for corrected geometry. Historical revisions
remain distinct from the current result.

Treat a metric attachment as evidence tied to a map and its geometry, not a
permanent conversion constant for every future map. A scale fit does not qualify
obstacle coverage, clearance, motion safety, camera-to-robot extrinsics or
metric velocity. Sparse features and semantic observations still do not form
an occupancy map.

### Remote request completion

For BiomeRTE, submit the named :1 contract with a unique operation ID and request
ID, then follow cap.status for that same operation. Local API names omit :1.
The completed telemetry object is at cap.status.result.result, including any
domain error. The adapter does not upload images for scale operations; request
an exact image explicitly through robot.vision.frame when it is needed.

A lost local response can occur after a session, reference, fit or attachment
was changed. Those mutation failures report effect=unknown and direct the
caller to inspect current state before issuing a new mutation. Read scale
status and the relevant session rather than blindly creating another session
or repeating an apply. Identical transport retries reuse their original
request ID and arguments; they do not become new mutations.

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
invalid-calibration behavior, metric evidence and qualification, and the
provider/installer boundaries. Adapter tests pin the original public schemas,
validate new numeric inputs through the real controller, preserve revisioned
metric telemetry, and retain mutation uncertainty after lost responses. Genuine
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
car. Every camera requires its own validated intrinsic and distortion record;
the supplied 0.0635 m height and provisional zero downward pitch do not replace
that calibration or a qualified map-scale reference.
