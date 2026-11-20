# Rover route recovery

Stable device identity and MAC are stored separately from current host in a local SQLite registry. Authenticated sensor sessions update camera routes. HTTP and RTSP reconnects resolve the current device entry. Gateway neighbor discovery updates only registered, unambiguous MAC identities.

Gateway configuration: device_routes names the SQLite file; device_route_provisioning maps device IDs to private sensor-push configuration files. When a camera or gateway address changes, the gateway reconciles the persisted WebSocket destination through the existing device configuration API. It reads configuration before retrying a possibly applied POST. Sensor readings remain pushed.

GET /device-routes requires a controller stream-router bearer secret and never exposes attachment tokens. NTP startup is independent of listener readiness; driving deadline checks still reject an unsynchronized clock. The listener remains open until server shutdown.

Sensor-only events have their own identity-checked stream contract. Camera frames retain their required metadata and payload checks. Live image publication timestamps do not claim completed SLAM analysis.

Rear capture/tracking uses a separate worker and the existing native map. Matching camera settings is distinct from intrinsic calibration. Rear map admission remains gated until real calibration and usable imagery are available.
