# Native host I/O boundary

The adapter keeps registered route ownership, host neighbor discovery, native
Unix vision/survey transports and LAN UDP source identity on the host.
Portable services use the same existing device registry through a narrow
authenticated API. No controller or vision SQLite file is mounted in Docker.

This adapter is **not** the staged service state authority protocol. It does
not certify a ToF checkpoint, obtain a service lease, fence a prior writer,
or make the Mac's JSON state available when the Mac fails. Those proofs must
come from the existing biomerte-service-adapter/1 handoff before activating
a stateful portable service.

## Authentication and host binding

The client follows service_adapters.AdapterClient's existing convention:
HTTPS by default and a private token_file. Local private HTTP must be
explicitly enabled. The server compares credentials in constant time,
requires an owner-only token file, and never logs request headers or bodies.
Keep credentials in a private named runner binding, never an image.

Server host_id is the stable registered host/controller reference. Client
host_id pins that identity. A route reference resolves the endpoint per
deployment; a host IP is never service identity. The native adapter's endpoint
is a host binding, not the provider placement.

Both examples are inactive templates. They do not create or replace a
launchd service. Replace each configured path through host-local bindings.
Use existing DeviceRoutes source and its existing database. Do not create
a fresh empty registry.

Run natively with:

    python -m biomerte.host_io --config /private/host-io-server.json

For Docker Desktop, host.docker.internal is a local transport binding. Check
reachability from the chosen container network before use. Loopback binding
remains the default; an explicitly chosen private interface/TLS listener is
required where the Desktop transport cannot reach it. Do not open a public
unauthenticated port.

## Client API

HostIOClient.from_config(path) returns a synchronous client.

- clock() returns ClockBounds, refreshing at most every three seconds.
- list_routes(), route(device_id), origin(url, device_id), candidates(bssid)
  read the authoritative registered routes and native neighbor candidates.
  Unknown routes fail closed in origin; a stale literal URL is not fallback.
- observe_anchor(bssid, host) retains the established ftm_anchor identity and
  route revision semantics. Effects must be explicitly active.
- refresh_routes() calls the native refresh_neighbors/provision_routes path.
  It is unavailable unless the host refresh loop is explicitly enabled.
- vision_frames(stream="live") yields (metadata, jpeg) for live/rear/map.
  All source metadata and JPEG bytes remain available. Known host monotonic
  fields receive an explicit bounded conversion; original values and local
  intervals are retained under _host_io_clock. Firmware monotonic counters
  and UTC capture/receive timestamps are not restamped.
- survey_zero(world_id, request_id) preserves the existing UUID/world/JSON-line
  protocol. It forwards to the configured active authority: current native
  survey-pose.sock or a specifically bound portable service endpoint.
- device_request(device_id, path, payload=None) resolves the current device
  route and retains native camera credentials. Only FTM and AHRS calibration
  paths are permitted; callers cannot choose a destination URL.
- anchor_datagrams() yields complete original UDP datagrams plus original
  peer address/port, source boot clock, bounded local receive interval, and
  sequence/gap indicators.
- send_responder(payload, address) sends only the existing validated
  rover.tof.responder/1 announcement to a native UDP peer seen within 30s,
  from the original collector socket/port.
- health() reports available native dependencies and activated paths.

All operations use fixed paths under /v1/host-io. No arbitrary filesystem
path, shell command, HTTP target, provisioning path, or credential is accepted
from a client.

## Timing contract

ClockBounds exposes host_id, clock_id, sampled_at, offset_low/offset_high,
valid, age(host_timestamp, source_clock_id=None, now=None), and
to_local_interval([low, high], source_clock_id=None).

A clock exchange bounds host-minus-client monotonic offset by client request
and response times. A conservative 500ppm drift allowance widens the interval.
Samples expire after 10s; uncertainty above 250ms, excessive UTC skew, a source
boot mismatch, malformed intervals, or a backward local clock reject the
measurement. The client does not reset a stale observation's capture time to
the moment it arrived.

A scalar converted timestamp uses the oldest possible observation. Range
intervals retain both bounds. Consumers must use the upper age bound for
freshness and reject unknown producer identity. Legacy stream fields without
clock_id require an explicitly verified source-host binding and invalidation
on boot or stream reattachment. The adapter's clock is not automatically the
clock of an arbitrary remotely routed stream.

## Native route loop and UDP cutover

After moving the router, configure route_refresh_interval_s=5 so host ARP
discovery and existing provisioning continue. The adapter invokes the exact
installed DeviceRoutes refresh/provision implementations and existing private
provisioning files. It never owns a parallel routing database. Leave this
disabled during isolated qualification.

UDP is disabled by default. ToF's original coordinator and the adapter must
never both own the collector endpoint. The adapter intentionally does not set
SO_REUSEADDR. At a qualified handoff: stop/fence the previous authoritative
coordinator, enable the adapter's original UDP port and effects, then activate
the one staged portable coordinator. A rollback reverses that order.
A Docker published UDP port is not an acceptable substitute until source IP
and collector reply identity are proven; the native relay avoids that NAT
ambiguity.

The server bounds UDP inboxes, frame sizes and subscription count. A dropped
UDP frame creates a visible sequence gap. A consumer must clear partial
legacy packet assembly at a gap; it must not join fragments from different
anchor reports.

## Qualification completed in the source change

Tests use temporary files, local TCP/UDP ports and Unix fixtures. They verify
authentication/effect gates, registered-route changes, source clock conversion
and uncertainty rejection, original metadata/JPEG forwarding, unchanged
survey request identity and UDP peer preservation. They do not activate a live
collector, write the production registry, send a device command, certify an
application state checkpoint, or qualify cross-host failover.
