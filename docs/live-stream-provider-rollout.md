# Live vision provider rollout

Preserve the deployed mapping release (0d42010e2b43), including revision-aware floor anchors and robot localization, when activating the previously saved streaming feeds. The original streaming branch predates those native navigation additions.

The service exposes owner-only Unix-socket `/v1/live` and `/v1/live/map` feeds from the existing processed-frame pipeline. The provider advertises `robot.vision.stream:1` as persistent and stream-only only when the running service reports both feeds available. Shutdown withdraws the offer. No browser owns the lease. Large map deltas become snapshot requests before exceeding the router metadata bound.

Install `providers/robot-vision/requirements-streaming.txt` into the existing vision virtual environment. Update both service/provider launch paths and the provider manifest `repo` to the same immutable release. Preserve state, lens calibration, maps, checkpoints, camera configuration and provider identity.

Qualification before rollout: native vision suite 437 tests (433 passed,4 optional native-app tests skipped);32 stream grant/router/wire tests passed, including real WebSockets. These results do not establish public ingress, physical relay timing or camera capture-clock accuracy.

The controller stream-router secret and public WSS URL are prerequisites for grants. The gateway additionally needs a reachable persistent router via STREAM_ROUTER_ORIGIN or a service binding. A URL/secret cannot substitute for persistent public hosting. The observed deployment has no such binding; public end-to-end streaming remains pending.

## Mac service and public gateway

The persistent owner runs as org.biomerte.stream-router beside the controller. `deploy/install-mac-stream-router.py` creates private configuration, preserves or generates the node signing secret, sets the public WSS descriptor URL, and installs a launchd KeepAlive service. Its private HTTP listener is restricted to loopback127.0.0.1:8751. The public gateway forwards WebSocket upgrades through the Sites private binding CUSTOMER_HTTP_STREAM_ROUTER. A separately registered Sites private-connectivity tunnel must target that listener; its exact logical tunnel ID is supplied as stream_router in deploy_site_version.tunnel_bindings. Registration and actual WebSocket forwarding must be verified; an unbound adapter is not live streaming. The controller must restart after node configuration changes. No per-message controller jobs or stateless in-memory gateway pairing is introduced.

Existing native provider routing uses invoke_provider and must be preserved. Only the canonical org.biomerte.robot-vision-provider service should own the provider worker lock; retire the duplicate biome.robot-vision-provider launch entry during this release activation.
