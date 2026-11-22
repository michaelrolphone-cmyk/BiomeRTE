# Portable Router and Rover candidates

This image captures the **effective deployed** stream Router and Rover frontend,
including world snapshots, sensor mailbox rules, logical service authorities,
origin-bound recovery, video APIs, survey zero and camera light controls.
It is built from committed source. `source-provenance.json` records the original
release file hashes and each packaged file hash. Native controller code is not
replaced by the older deployment closure.

## What can run where

| Component | Container boundary | Placement qualification |
| --- | --- | --- |
| Stream Router | Existing authenticated HTTPS stream inspection, bounded UTC clock, host I/O route lookup | Stateless broker candidate; preserves signed leases, stream epochs, sequence checks, intent expiry and latest-only delivery |
| Rover frontend | Signed controller HTTP, host I/O routes/survey/processing clock, separate recovery state | Executable single-host Docker candidate; automatic placement intentionally blocked until recovery-state replication/activation is qualified |
| Device routes, ARP and telemetry provisioning | Existing native registry, exposed by `deploy/host-io` | Remain host I/O; never copy the registry or bind native SQLite into a Linux container |
| Native controller, independent maintenance and updater | Existing native launch services | Remain recovery infrastructure |

A container image alone does not establish failover. Every intended host needs
its own prepared image, private configuration, live dependency routes and any
required state/fence proof. No manifest fixes a workload to a Mac/Pi hostname.
The Pi has not been qualified for these workloads while offline.

## Build and inspect

Build from repository root with a specific source revision:

```sh
docker build -f deploy/portable-services/Dockerfile \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  -t biomerte-portable-services:local .
docker image inspect biomerte-portable-services:local
```

The default Python base is pinned to the digest already qualified for the
existing service-authority image. Application dependencies are pinned to
aiohttp 3.14.5 and websockets 15.0.1. The image contains no credentials, native
databases, provisioning JSON or device inventory. Registry credentials remain
outside the build context. The shared image supports distinct Router and Rover
entrypoints. Prepare separate immutable images for each platform; classic
Docker image stores cannot load a combined multi-platform manifest.

Use the repository image build/export pipeline for architecture-specific local
images and later digest-pinned registry publication. A registry is not required
to prepare or test local immutable image IDs.

## Router configuration and handoff

Create private configuration from the installed settings, using
`router.example.json` as the shape. Substitute real registered authority IDs
and existing secrets without printing them. The example is deliberately not a
live declaration. Native controller validators use either the existing authenticated HTTPS
`stream_inspection` endpoint or the already deployed Rover owner inspection
endpoint. Credentialed owner inspection refuses redirects and ambient proxies.
The latter retains both existing derived credentials and permits
private HTTP only with explicit opt-in. The observed Mac has no dedicated
public authority inspection link, so its initial Router migration must use
`http://host.docker.internal:8093/internal/stream/inspect`; pointing it at the
public `/stream/inspect` route would incorrectly make live streams unavailable. Logical service authorities
retain the existing derived credential and service inspection mechanism.
The native controller database is not opened and `Application` is not imported by this
entrypoint.

`private_docker_http` requires explicit `network_scope: private`. The supplied
manifest publishes only to host loopback and joins the existing protected
Docker network. Plain WebSocket service names must be explicitly allowed in
the Rover configuration. For other exposure, configure TLS; the original
native loopback-only HTTP behavior remains unchanged.

Use `prepare_router.py` to read existing private settings into a new private
candidate directory. It opens only JSON/key files, never a database, and never
changes installed configuration:

```sh
python3 deploy/portable-services/prepare_router.py \
  --source-config /native/router.json --output-dir /private/candidate-router \
  --owner-inspect-url http://host.docker.internal:8093/internal/stream/inspect \
  --allow-owner-local-http --host-io-url http://host.docker.internal:8765 \
  --allow-host-local-http --host-io-token-file /private/host-io.key \
  --host-id REGISTERED_HOST_ID
```

The native host adapter can stay bound to loopback on Docker Desktop; that
connection has been checked using an authenticated isolated fixture. Keep the
candidate private directory outside the build context and mount it read-only.
The tool refuses to overwrite an existing candidate directory.

During qualification run the host I/O service with route refresh disabled
(`route_refresh_interval_s: 0`) so the native router remains the sole provisioning
writer. At cutover, stop the native router first, then enable the existing
ARP refresh and telemetry provisioning loop on host I/O at five seconds. It uses the original
DeviceRoutes database in the native OS. The container's `/device-routes`
endpoint only reads through this authenticated adapter and keeps the original
router bearer check. It does not create a second registry or write device routes.

Generate the existing service-runner schema after obtaining actual image IDs:

```sh
python3 deploy/portable-services/manifest.py \
  --image linux/arm64=sha256:ACTUAL_IMAGE_ID \
  --host-port 8751 --output /private/staging/stream-router.json
```

This computes and validates the complete canonical manifest revision.
The shared declaration uses a logical `stream-router-private` read-only
binding. Resolve that binding in each host's existing runner configuration.
Do not declare the sample with fake digests. The generator does not register,
start or stop anything.

Before live cutover: validate private configuration; test the container on an
unused loopback port; verify inspection, control UTC and host I/O; preserve
existing provider and outbound bridge endpoint references; stop the old router; enable the
native host adapter's five-second route refresh; then declare the qualified
container. Do not overlap the old and new provisioning writers. Keep the old release/config/launch definition for rollback.
A rollback drains/removes the new runner child, disables host I/O route refresh,
then restores the old native listener. The native and host-adapter provisioning
loops must never run together.
No source/build/test command in this directory performs that cutover.

## Rover state, clocks and effects

`rover.py --config /config/rover.json` provides the executable Docker entrypoint.
`--check-config` validates its shape without acquiring capabilities, connecting
the public bridge or changing devices. Configuration separates read-only
controller identity/configuration and paired caller keys from `/state`.
The controller endpoint is explicit and still receives exactly the native
signed envelope. Private HTTP requires explicit opt-in; public endpoints use TLS.

`pending-routes.sqlite` records an operation's original authority **before**
sending it. A lost reply must remain bound to that origin after restart,
even when new work resolves to a different owner. Therefore:

* Keep it in a dedicated durable Docker state volume.
* Transfer a consistent copy only after the native Rover is quiescent; preserve
  pending rows and receipts exactly. Never clear an uncertain operation.
* Do not mount an actively used native SQLite/WAL directory through Docker
  Desktop, label it cache, or declare the service stateless.
* Distributed activation additionally needs a qualified checkpoint/custody
  adapter and exact grant/fence handoff. The entrypoint explicitly refuses the
  runner's service environment until that integration exists.

No ready-made Rover runner manifest is emitted, because its recovery-state
adapter has not been qualified. A Docker restart on one host and a cross-host
takeover have different custody requirements.

Vision processing timestamps originate on the native vision host. They cannot
be subtracted from Linux VM monotonic time. Rover samples the authenticated host
clock asynchronously, uses conservative offset bounds, and preserves the
original processing-to-server freshness limit of 0.5 seconds. Delayed, future,
stale-clock and previous-boot frames are rejected. No received frame is stamped
fresh. The source host and stream resource are explicit private configuration.
Legacy untagged processing timestamps require an explicit verified host
assertion; a host boot change invalidates that stream association until a new
attachment. Producers should add `processing_clock_id` to remove this legacy
assertion in a subsequent compatible rollout.

Survey zero uses the same world/request UUIDs and acknowledgement semantics
through the host adapter. The adapter must target the **current authoritative**
survey writer; moving that writer requires its own state handoff. For a native
Rover bridge with a portable estimator, install the shared `host_io_client.py`
in the bridge's `biomerte` package and configure the estimator's existing
private binding in `~/.local/share/biome-rover-server/survey-estimator.json`.
Native estimators without this binding use their original Unix socket. Camera-light
requests resolve the configured device identity through the existing route
registry every time and never fall back to a frozen IP. Light confirmation
remains command-state confirmation, not optical verification. Control intent,
stop/repress behavior and provider readiness rules are unchanged.

## Verification

```sh
python3 -m unittest tests.test_portable_services tests.test_host_io -v
```

The portable suite executes against the isolated captured package and the one
client file copied by Docker. It checks signed destination preservation, lost
submission custody across restart, all current UI routes, native/container
clock epochs and reboot rejection, control expiry/epoch fences, dynamic device
route changes, survey acknowledgement semantics and source provenance. Native
tests and a container smoke test do not claim a physical Pi/Mac failover or
relay board acknowledgement.
