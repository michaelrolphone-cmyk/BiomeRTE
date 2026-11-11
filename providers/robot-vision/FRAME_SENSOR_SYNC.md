# Frame-aligned sensor input

Set `camera.synchronized_capture: true` only after the camera serves the
`biome.capture.v1` endpoint documented by firmware `1.0-radio.8-sync`.
The default RTSP reader remains available. The synchronized reader runs in its
own camera thread and publishes only the latest complete image/sensor packet.
It makes no network calls from the vision-frame processing loop.

Each observed frame retains the real device capture timestamp, receiver/boot
identity, AHRS sample times and Wi-Fi/BLE observation times. Relative native
SLAM timestamps use capture intervals, with a fixed host epoch anchor per boot.
Decode time remains separately labeled. Boot changes keep tracking time
monotonic without changing the map identity.

AHRS evidence is frozen before visual tracking from the same image packet.
Radio training/estimation uses the packet's observation times against the
corresponding capture-time visual pose history. Unlocalized frames do not
become fabricated radio training positions. Live AHRS values are correlated to
the displayed frame; old independently polled values cannot masquerade as that
frame's measurements. Legacy telemetry polling remains for health, explicit
sensor APIs and camera control.

The camera SDK timestamp marks the first DMA buffer, not rolling-shutter
mid-exposure. Packet age and host-clock bounds are reported separately. The
camera-to-IMU transform and inertial position/metric scale are not established
by temporal association; the native estimator remains monocular. No map reset,
pose fabrication, gyro calibration or motor action is part of enabling this.

The unknown-area recovery gate also counts actual native frame keypoints
(`len(frame.kps)`), rather than a nonexistent `num_kps` attribute. Native status
reports this count so a textured scene cannot silently appear featureless to
the recovery policy. The map/checkpoint retention policy is unchanged.
