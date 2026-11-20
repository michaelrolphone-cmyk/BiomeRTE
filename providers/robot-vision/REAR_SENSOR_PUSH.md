# Rear sensor socket
The rear uses the same wire protocol as front firmware PR245: authenticated
outbound WebSocket to /sensors, clock request/reply, biome.sensor_tick.v1 and
boot-scoped sequence acknowledgements. It has a separate listener, token and
receiver identity. It never updates front AHRS/sonar sources or creates a map.

Create rear-sensor-push.json in the vision service state directory:
```json
{
  "listen": "0.0.0.0",
  "port": 9878,
  "token": "<rear-specific secret of at least 32 characters>",
  "receiver_id": "rear-<actual efuse identity>",
  "ftm_peers": [
    {"address": "aa:bb:cc:dd:ee:ff", "channel": 6}
  ]
}
```
Use actual discovered anchor AP BSSIDs and channels, not the example. Omit the
ftm_peers list when the existing coordinator schedules rear initiations.
Read receiver_id from rear /api/telemetry; configure the firmware's
/api/telemetry/stream with the matching gateway address, port and secret.
Listener configuration is loaded at service startup; camera mounting remains
runtime configurable through the existing camera-rig.json/capability.

The rear listener starts even when rear tracking is disabled or intrinsics
are not calibrated. Clock-qualified telemetry is published to the existing live
stream with camera_id=rear, original sensor packet, LiDAR/FTM/UART and the SAME
map_id/revision/session as the front. It claims no image or pose for sensor ticks.
Status appears under robot.vision.rig.status sensor_push. Stale snapshots retain
their original clock interval and receipt age; reads never rejuvenate them.

Optional rear-ftm-schedule runs on its own thread, uses the rear camera URL's
HTTP credentials, waits for each queued session's report identity, and never
blocks either tracker or the socket loop. No retry implies a completed range.
The C3 fleet can range to the rear responder independently; the rear initiates
to configured peers, providing both directions. Missing Wi-Fi, authentication,
LiDAR, UART or socket readiness leaves the existing front pipeline running.
The rear hardware must still be flashed, configured and physically qualified.
