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
- STATE/radio/radio.sqlite3 holds the optional bounded radio model separately
  from SLAM checkpoint packages.
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

All 26 names below have version :1. The original 13 envelopes are unchanged;
the eight scale/metric-read and five radio/light contracts are additive. The BiomeRTE adapter
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
| robot.radio.status | include_observations=false | Cached source freshness, receiver/model/learning health and optional exact telemetry envelope |
| robot.radio.localize | none | Current coarse radio hypotheses, competing modes, heuristic support and explicit abstention |
| robot.map.radio.read | map_id, revision, source_id, cursor (opaque string), limit | Learned radio distributions resolved at a pinned visual-map revision, with independent model revision and guarded pagination |
| robot.camera.light.status | none | Explicit firmware light command/driver state with physical-readback qualification |
| robot.camera.light.set | nonempty partial on, brightness, rgb | One explicit authenticated light command; firmware result and unknown-outcome semantics retained |

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


## Adaptive radio fingerprints

Wi-Fi and BLE observations provide coarse evidence about previously visited map
regions. The radio model learns a conditional RSSI distribution for each source
and spatial region, then compares a new fingerprint with those distributions.
It can suggest native keyframes for visual relocalization and report whether a
coarse radio estimate agrees with a valid visual pose. Its score is a heuristic,
not a calibrated probability or a measured transmitter range. It never sets
visual localization to valid, supplies a heading, or grants navigation authority.

### Configuration and firmware contract

The HTTP client implements the
[pinned ESP32 camera radio/light contract](https://github.com/michaelrolphone-cmyk/RiscRTE-System-Apps/blob/801fd5ddb1a4685c4a9a6d999a430806b96835b4/firmware/esp32cam-rtsp-radio/README.md).
It polls `GET /api/telemetry`; it does not enable scans, change firmware radio
settings, or subscribe to the firmware's optional UDP output. Firmware scan
periods and retained observation ages therefore remain independent of the
host polling interval.

Radio polling and learning are opt-in. Existing configurations without a
`radio` section behave as `enabled: false`. Merge a section like this into
`STATE/config.json` when installing the feature on compatible firmware:

```json
{
  "radio": {
    "enabled": true,
    "receiver_calibration_id": "rssi-v1",
    "poll_interval_s": 5.0,
    "network_timeout_s": 2.0,
    "freshness_threshold_s": 15.0,
    "prior_max_age_s": 5.0,
    "pose_history_s": 180.0,
    "pose_history_limit": 1200,
    "mapping": {
      "max_source_age_s": 15.0,
      "max_fingerprint_span_s": 1.0,
      "max_pose_time_delta_s": 0.5,
      "spatial_bin_size_map_units": 0.5,
      "cluster_radius_map_units": 1.0,
      "independent_sample_interval_s": 1.0,
      "min_anchor_samples": 3,
      "min_source_reliability": 0.15,
      "decay_half_life_s": 86400.0,
      "retention_s": 1209600.0
    }
  }
}
```

`radio.mapping` accepts the model settings defined in
[radio_map.py](radio_map.py); unknown mapping keys are rejected.
`receiver_calibration_id` belongs directly under `radio`, with default
`rssi-v1`. Give a changed antenna, radio gain, or receiver RSSI calibration a
new identity so incompatible receiver statistics remain separate. Camera lens
calibration identity and firmware receiver identity are also retained.

By default, the HTTP origin uses the camera host: an RTSP URL uses HTTP port 80,
not the RTSP port. An optional `radio.base_url` may select an HTTP(S) origin on
that same host. Authentication uses the existing camera URL or explicit camera
username/password configuration; the client does not copy credentials into
telemetry or redirect them to another host. HTTP requests run in a background
poller with a latest-only buffer. Radio model work also runs outside the visual
tracking path. Model storage failure is reported through radio status while
vision continues; invalid configuration remains an explicit startup error.

### Learning a changing RF landscape

Training requires a successfully published, independently valid visual pose in
the same map and calibration, a retained keyframe with the same capture identity,
and a compatible radio observation interval. A radio-predicted location cannot
label its own training sample. A radio-prior-assisted visual recovery can
contribute only after the normal visual geometry checks independently accept its
pose. A model with no qualifying visual observations remains empty.

The model separates spatial variation from instability. It divides positions
relative to each native keyframe into spatial cells and learns each source's
RSSI mean, variance and reliability within those cells. A source that differs
between places can remain useful; variation within one place and weak or aged
support reduce its reliability. RSSI observations decay with a one-day default
half-life and expire after fourteen days. New independently anchored visits can
therefore adapt the model to changed transmitters, people and environmental
conditions. It does not identify people, classify a transmitter as moving, or
infer the physical cause of a changed signal.

The default cell edge is 0.5 raw map units. The effective edge is capped at
`cluster_radius_map_units / sqrt(3)`, so one cell cannot silently pool positions
farther apart than the spatial grouping scale. These are heuristic scales in
the existing monocular coordinate system, not metres. Map reads expose
`region_id`, `spatial_cell`, effective cell size, visual frame count, sampling
epoch count, decayed weight and maturity.

Repeated scans retain useful variation without becoming repeated independent
pose confirmations. Records sharing a visual frame or a one-second default
sampling epoch share their learning weight. By default, a region needs at
least three visual frames, three sampling epochs, and three overlapping sources
with reliability at least 0.15 before it can support a coarse position.
Insufficient support returns `insufficient_model_support` with
`position: null`; candidate evidence remains available.

Comparison uses a reliability-weighted mean residual. It estimates a bounded
common RSSI offset using a weighted median, with a default maximum magnitude of
6 dB, and penalizes that correction. Common attenuation across several radios
is not multiplied into several independent confirmations. The implementation
does not claim that this simple correction explains every multipath,
orientation or environmental effect.

Wi-Fi identity uses BSSID rather than SSID. BLE keeps address type and address,
or a supplied iBeacon UUID/major/minor identity, with its stability provenance.
Names are not identities; a beacon advertisement is not authenticated proof of
a fixed transmitter. Rotating/private addresses can lose continuity. Missing
a transmitter from one scan is not proof that it moved or that the robot left
a place.

All retained matching spatial regions are assessed for competing modes before
the returned-candidate limit is applied. Complete-link grouping requires every
pair in one mode to lie within the configured radius, preventing a chain of
nearby matches from joining distant places. If a second separated mode has at
least 0.8 of the best mode's similarity by default, the result is
`ambiguous` with `position: null`. The output retains bounded candidate/mode
summaries, total counts and truncation flags. It never averages two distant
plausible places into an asserted midpoint. Reported covariance and spread
describe the retained conditional observations, not a physical accuracy bound.

### Observation timing and coherent map evidence

A fresh telemetry response can contain old RF measurements. The client retains
firmware receiver/boot identity, source observation counters and per-source age;
cached ages continue advancing with the Mac monotonic clock. Repeated HTTP
publication does not create a new RF sample or refresh its acquisition time.
Persistent per-source watermarks reject replay, including across host restarts,
and retired receiver boots are not treated as new measurements.

The model estimates each radio observation's host time from receipt time minus
firmware age and half the HTTP request duration. Half the request duration is
retained as transport uncertainty. It groups sources within a one-second default
span and requires each source's age-aware interval to align with an independent
visual pose within 0.5 seconds by default. Late visual processing can retry the
join against bounded pose history; an old RF payload still cannot be trained
again merely because a new visual result arrived.

This is explicitly approximate host receive/decode alignment. The firmware
stream provides no synchronized camera exposure timestamp, and
`physical_capture_time_uncertainty_s` remains unknown. These limits are
policy thresholds, not measured synchronization accuracy. Consumers must inspect
the returned observation age, expiry, clock qualification and availability.
A radio estimate newer than the visual frame is not presented as coherent
support for that earlier frame.

Fingerprints are partitioned by map, lens calibration, receiver and receiver
calibration identity. Positions are stored relative to their native keyframe
and resolved against the requested map geometry on read. Loop corrections can
therefore update their map positions without rewriting radio samples. A culled
anchor, or reuse of its numeric ID for a different captured frame, cannot
silently rebind the evidence. Context changes invalidate cached predictions
until the matching geometry is available again.

`robot.map.radio.read` is the dedicated layer reader. Its `revision` input
pins the visual map revision; the response also carries the independent radio
model revision and evaluation time. Pass the returned opaque `next_cursor`
string unchanged, together with that same visual map revision and query.
Pagination fixes the decay evaluation time and rejects changed model/geometry
state; restart the traversal on a changed cursor. The radio layer retains raw
map units even if a separate metric attachment exists.

### Bounded persistence and visual relocalization

Learned data live in `STATE/radio/radio.sqlite3`, separately from observation
SQLite backups and backend checkpoint packages. Radio state is not copied into
every SLAM checkpoint. Default bounds are 512 retained training records,
16 records per native anchor shared across its spatial cells, 128 native
anchors, 16 receiver/map contexts, and 96 source observations per fingerprint.
Retention preserves recent and temporally distributed records with spatial-cell
diversity, then recomputes statistics from the retained history.

The main SQLite database is capped at 16 MiB by default. SQLite's delete-mode
rollback journal may temporarily require additional disk; the limit is not a
bound on every temporary filesystem byte. There is no persistent WAL. Replay
protection is separately bounded at 4,096 source watermarks and 64 boot records
per receiver, with thirty-day default retention. Exhausted deduplication or boot
capacity fails closed instead of forgetting protection and relearning stale
events. Status exposes counts, settings, database size and model errors.

Fresh radio candidates can supply a first visual relocalization pass. The
backend checks current retained native keyframe IDs before limiting the pass,
then uses unchanged descriptor, correspondence, PnP and geometric acceptance
checks. If that pass fails, normal visual place recognition still runs.
`radio_prior.supported`, candidate IDs and the last native result distinguish
available source support from an actually exercised successful recovery.

Installing the Python feature alone does not install the native prior hook.
Run the existing `setup_slam.py --phase patches` step against the intended
state directory and load the patched backend through the normal service
lifecycle before expecting the first pass. An older native backend reports
`radio_prior.supported: false` and continues ordinary visual operation.
Fixture and transform tests do not establish live RF accuracy, actual camera
firmware compatibility, or successful relocalization on the robot.

## Manual camera lighting

`robot.camera.light.status` performs the firmware's authenticated
`GET /api/light`. `robot.camera.light.set` performs one explicit
`POST /api/light`; it works independently of whether radio polling is enabled.
A command is a nonempty partial object containing only `on` (JSON boolean),
`brightness` (integer 0..255), and/or `rgb` (three integer bytes in RGB order).
The serialized body is limited to 256 bytes. For example:

```json
{
  "operation": "robot.camera.light.set",
  "inputs": {"on": true, "brightness": 128, "rgb": [255, 255, 255]}
}
```

An explicit `{"on": false}` update turns it off. In the pinned firmware,
off remembers color and brightness while transmitting black; brightness zero
also produces black. State is held in RAM and the firmware starts off. The
contract has no automatic illumination-duration limit. Its selected hardware
profile is one WS2812-compatible RGB pixel on GPIO48; GPIO4 is camera SCCB data,
not a flash output.

Frame metadata includes mean image luma and a low-light flag (mean below 35 on
the 0..255 scale). This is an image-brightness observation, not measured lux or
an automatic light command. A caller can request light and inspect a new frame
to assess the result. The software does not turn lighting on merely because a
frame is dark.

Light results preserve firmware command/driver state, `hardware_verified`
and `optical_readback`. A successful HTTP response or ready driver does not
prove that the physical LED is present or that the scene is illuminated.
Firmware errors and uncertain POST outcomes are returned explicitly. A lost
response, failed driver receipt or invalid response after a write must not be
treated as proof that no command occurred; inspect command state before an
intentional retry. The client never automatically retries a light mutation.

Neither radio location evidence nor an illuminated image establishes dark
obstacle coverage, free space, metric depth, or safe driving. The existing
geometry and navigation availability rules continue to apply.

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
provider/installer boundaries. Radio tests cover source/boot replay, time/pose
alignment, adaptive spatial distributions, competing modes, bounded retention,
manual light errors/uncertainty, and native prior fallback with unchanged visual
acceptance gates. Adapter tests pin the original public schemas,
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

## Calibration recovery and low-contrast input

Checkerboard fits resolve observations against the requested map revision.
An explicitly deleted anchor can be excluded only when its capture revision
and uninterrupted live history prove the same keyframe image identity and
calibration. Unknown, replaced or ambiguous anchors remain unresolved.
The candidate's `anchor_resolution` reports surviving training/validation
counts and exclusions; all original samples and their roles stay preserved.
The normal baseline, held-out validation and physical-size checks run again.
Measured-distance endpoints remain strict and are never silently substituted.

SLAM can optionally use an explicit contrast profile in `config.json`:

```json
{"slam":{"preprocessing":{"mode":"percentile_gray","version":1,
                         "low_percentile":1.0,"high_percentile":99.0}}}
```

The default mode is `none`. The optional profile converts BGR to grayscale,
stretches the configured intensity percentiles globally, and gives SLAM three
identical channels without moving or resizing pixels. Flat images remain flat.
Raw frame evidence and detector inputs retain their original bytes.
Status reports the active/loaded policies, descriptor-policy history and
brightness/blur diagnostics measured on the raw image. Contrast gain also
amplifies noise; it does not qualify optical sharpness or localization.

Checkpoint manifests distinguish the active profile from profiles that have
contributed keyframe descriptors. Legacy maps retain their raw-profile history;
a configured profile transition must freshly relocalize and cannot relabel old
descriptors. A map may contain point colors from grayscale SLAM inputs.

A featureless camera frame is retained as zero-length feature and association
arrays. The native persistence qualifier includes a real blank input before
saving, checks that its identity and empty arrays survive restore, and then
requires a real non-keyframe image to relocalize. An empty image never supplies
a current pose or deletes the initialized map.
