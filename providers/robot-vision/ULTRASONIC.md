# Four HC-SR04 distance channels in the rover stream

The ESP32 camera firmware from RiscRTE-System-Apps PR214 adds authenticated
GET /api/ultrasonic with schema1 / type ultrasonic and hcsr04.v1 sensor data.
This provider update requires that firmware for actual values. It never flashes
a device. Existing firmware remains supported: a missing endpoint is reported
unavailable and probed every5seconds while camera/AHRS/radio operation continues.

UltrasonicSource owns a separate background HTTP poller (default260ms interval,
1second network timeout). VisionService frame delivery only reads its latest
in-memory snapshot. Sensor caches are not queued or journaled separately.

Settings use the optional ultrasonic configuration object:
enabled=true, poll_interval_s=0.26, network_timeout_s=1.0,
freshness_threshold_s=1.0. Old configurations receive these defaults.
Authentication and origin validation reuse CameraHTTPClient.

Live vision frame metadata carries an ultrasonic envelope with original
receiver_id, boot_id, uptime_ms, sequence and four sensor slots, plus snapshot
receipt timing and per-slot effective_age_ms references. Effective age adds
firmware age, time since HTTP receipt, and the request duration as a conservative
transport allowance. Repeated video frames never reset measurement age.
The GUI also adds browser elapsed time and hides numerical values above1000ms.

Each slot retains id (front/left/right/rear), sequence, valid, status, distance_m,
echo_us, observed_uptime_ms and age_ms. Invalid slots require null values.
A timeout cannot distinguish no returned echo from a disconnected module.
Range directions describe the intended mounts; no calibrated sensor extrinsics
or automatic obstacle braking are implied.

Stream compaction removes duplicate envelopes before trimming RF scan rows.
All four range values and their timing references survive compaction, or the
existing bounded-header check rejects an oversized message.

Validation:453 existing/new provider tests ran on the Mac with26skipped and no
failures; two additional live-frame integration tests verify cached values
reach a JPEG frame without HTTP calls on the frame path and old firmware has
explicit unavailable state. Site29 adds a four-direction visual display and
passes174tests. Physical values remain unverified until firmware installation.
