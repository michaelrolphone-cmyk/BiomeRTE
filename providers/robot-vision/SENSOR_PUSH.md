# Independent rover sensor push

The ESP32 initiates one persistent authenticated WebSocket to its configured native
gateway at `/sensors`. The gateway's existing vision stream distributes the same
sensor packets to navigation and GUI consumers. Camera frames keep their original
capture timestamps and do not determine the sensor delivery cadence.

Authenticated `POST /api/telemetry/stream` provisions `host`, `port`, and `token`.
Configuration persists in the separate `rover-push` NVS namespace. `GET` reports
connection state and counters without disclosing the token. Calibration remains in
`rover-ahrs`; no calibration or survey writes occur in this path.

Every 100 ms, the sender emits `biome.sensor_tick.v1`, explicitly with no image.
Packets include boot identity, increasing stream sequence, device monotonic capture
and service timestamps, and actual measured IMU samples since the acknowledged
sample. The PSRAM history can recover up to 2.5 seconds within its physical capacity.
Longer outages remain explicit observation gaps. An acknowledgement advances only
after the native receiver validates and publishes the batch. Reconnects resend
retained samples; sample identity prevents reintegration.

The native receiver sends `{type:"clock",id:...}` once per second. The ESP32 replies
with the same ID, boot identity and device receipt/service times. Host send/receive
times bound the device-to-host clock mapping; uncertainty broadens between exchanges.
Delayed samples retain their measured time rather than being stamped fresh at receipt.

`GET /api/sensors/capture` is also registered for authenticated diagnostics and older
clients. Normal configured delivery uses WebSocket push, without sensor HTTP polling.
The native LAN ingress accepts only the provisioned device and token and carries no
drive commands. Existing authenticated gateway connections carry data onward to the GUI.
