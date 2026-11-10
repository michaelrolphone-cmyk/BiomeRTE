# Live vision provider rollout

Preserve the deployed mapping release (0d42010e2b43), including revision-aware floor anchors and robot localization, when activating the previously saved streaming feeds. The original streaming branch predates those native navigation additions.

The service exposes owner-only Unix-socket `/v1/live` and `/v1/live/map` feeds from the existing processed-frame pipeline. The provider advertises `robot.vision.stream:1` as persistent and stream-only only when the running service reports both feeds available. Shutdown withdraws the offer. No browser owns the lease. Large map deltas become snapshot requests before exceeding the router metadata bound.

Install `providers/robot-vision/requirements-streaming.txt` into the existing vision virtual environment. Update both service/provider launch paths and the provider manifest `repo` to the same immutable release. Preserve state, lens calibration, maps, checkpoints, camera configuration and provider identity.

Qualification before rollout: native vision suite 437 tests (433 passed,4 optional native-app tests skipped);32 stream grant/router/wire tests passed, including real WebSockets. These results do not establish public ingress, physical relay timing or camera capture-clock accuracy.

The controller stream-router secret and public WSS URL are prerequisites for grants. The gateway additionally needs a reachable persistent router via STREAM_ROUTER_ORIGIN or a service binding. A URL/secret cannot substitute for persistent public hosting. The observed deployment has no such binding; public end-to-end streaming remains pending.

## Mac service and public gateway

The persistent owner runs as org.biomerte.stream-router beside the controller. `deploy/install-mac-stream-router.py` creates private configuration, preserves or generates the node signing secret, sets the public WSS descriptor URL, and installs a launchd KeepAlive service. Its private HTTP listener is restricted to loopback127.0.0.1:8751. The public gateway forwards WebSocket upgrades through the Sites private binding CUSTOMER_HTTP_STREAM_ROUTER. A separately registered Sites private-connectivity tunnel must target that listener; its exact logical tunnel ID is supplied as stream_router in deploy_site_version.tunnel_bindings. Registration and actual WebSocket forwarding must be verified; an unbound adapter is not live streaming. The controller must restart after node configuration changes. No per-message controller jobs or stateless in-memory gateway pairing is introduced.

Existing native provider routing uses invoke_provider and must be preserved. Only the canonical org.biomerte.robot-vision-provider service should own the provider worker lock; retire the duplicate biome.robot-vision-provider launch entry during this release activation.

## Full-spec audit qualification, 2026-11-10

The runtime audit pins lifetime/watch/fence/reservation policy on each lease, preserves explicit release-after-result false, enforces paired owner identity, and expires operational availability notifications without knowledge revisions. Selected expired records remove only their exact retention marker. Authoritative lease policy accompanies claimed work. The integrated supervised runner retains process custody across delivery loss, reports stalled output separately from heartbeats, and reconciles stopped children after controller abandonment. A proven adapter launch failure is terminal failure with no-execution evidence. Generic long commands use a private process group for explicit stop escalation; connectivity loss alone does not stop a child.

Qualification: 421 runtime tests passed on the Mac, including real supervised child heartbeat loss, abandonment, late custody recovery, replacement fencing and an uncooperative child stop. Live local-router qualification delivered three actual JPEG frames to two Rover subscribers through one server-held lease. The subscribers received identical frames; processed-frame age was 7.4–8.9 ms at reception. Detaching each subscriber preserved the lease; explicit server shutdown released qualification leases. Camera source capture-clock accuracy remains unknown. No image bytes were retained by this qualification.

The live local test overrides only the descriptor transport to the Mac loopback router. It does not prove public gateway traversal. Gateway Site version 66 contains the private-tunnel adapter and passed 284 gateway tests; production still requires an authenticated stable tunnel/origin. No existing eligible private tunnel or named tunnel credentials were available. Public WebSocket acceptance, physical relay timing, reconnect/restart under public transport and final full-system acceptance remain unqualified. Neither ESP32 was reflashed during this audit.
