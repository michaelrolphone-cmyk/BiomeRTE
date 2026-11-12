# Independent live camera delivery

The live camera subscriber reads CameraSource's latest-frame buffer in its own
thread. It forwards the original synchronized-capture JPEG, camera clock and
frame-associated AHRS/RF data. It does not wait for detection, SLAM, checkpoints
or the observation database. Slow subscribers retain one newest packet.

The existing sequential vision-processing worker and map persistence continue
unchanged. Its completed results update an analysis reference; they do not
publish an older image back onto the live lane.

The daemon also defers native backend/map restoration into the processing
worker. Its API, capture and live publisher start first. A slow or failed SLAM
startup leaves video available with explicitly unavailable current-frame pose.

Live metadata identifies the displayed capture with `frame_id`. Pose/detections
at the top level are usable only when processing completed that exact frame;
otherwise pose is explicitly unknown and detections are empty. The `analysis`
object carries its own processed frame ID, timestamps, map binding and pose.
Consumers can show that as the latest processed map observation, but must not
draw its image geometry over a different live capture. Full processed snapshots
and detection results remain available through their existing capabilities.

The v1 leased-stream sender checks `processed_monotonic` for image freshness.
On this camera lane that watermark is the capture's host receive/decode time,
explicitly labeled by `processed_monotonic_basis`. It is never reset by slow
SLAM completion. Actual analysis timing remains in `analysis`.

`robot.vision.status.streaming.camera_delivery` reports capture publications,
processing results, processing age and publisher errors independently.

Regression coverage holds the real VisionService backend and pipeline lock
blocked while newer JPEGs are delivered. It also verifies latest-only queues,
exact source-JPEG forwarding and rejection of stale analysis updates.
