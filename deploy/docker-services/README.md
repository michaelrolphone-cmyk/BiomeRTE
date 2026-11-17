# Docker service runner

This runner deploys declared services through the local Docker Engine API. Run
one supervisor container per controller host. The existing public Biome stream
gateway is the independent placement witness, so loss of either Pi or Mac does
not remove the placement authority.

The runner never changes Biome capability lease semantics or silently copies
controller databases. Stateless services can be automatically placed on either
eligible host. Physical or stateful services additionally require the fence and
state adapters described below.

## Manifest and configuration

Images are selected by Docker Engine platform, not host name. Registry images
must use immutable `repository@sha256:digest` references. An immutable local
`sha256:image-id` is also accepted, but must already exist on every eligible host.
The image is prepared before a host is advertised as eligible.

Every manifest has `id`, `images`, `effect_policy`, `state_policy`, and a
`revision` equal to SHA-256 of its complete canonical JSON excluding revision.
Compute with `biomerte.docker_services.revision(manifest)`. Optional fields:

- `command`, `entrypoint`, `environment`, `working_dir`
- `mounts` with absolute source/target and read_only; writable local binds must
  explicitly be disposable `cache_only`, never unreplicated authoritative state
- `ports` with container/host/listen/protocol; default listen is loopback
- `readiness`: HTTP dependency probes; `health`: in-container argv health check
- `lease_ttl_s`, `worker_ttl_s`, `startup_grace_s`, `preferred_workers`
- `network`, `extra_hosts`, optional `memory_bytes`, `read_only_root`
- `log_max_size` and `log_max_files`, default rotating Docker logs 8m × 3

A host-local runner configuration contains:

```json
{
  "worker_id": "controller-identity",
  "coordinator_url": "https://existing-stream-gateway.example",
  "coordinator_key_file": "/config/coordinator.key",
  "docker_socket": "/var/run/docker.sock",
  "manifest_files": ["/config/services/example.json"],
  "state_file": "/state/current.sqlite",
  "heartbeat_interval_s": 1,
  "request_timeout_s": 3
}
```

The coordinator key is the hexadecimal HMAC-SHA256 of the existing
STREAM_ROUTER_SECRET with message `biomerte/service-coordinator/v1`. Provision it
through a private, read-only mounted file. Do not commit it or put it in the
manifest, image, logs, command arguments, or API URLs.

Build from repository root:

```sh
docker build -f deploy/docker-services/Dockerfile -t biomerte-service-runner .
```

Run the supervisor with a read-only configuration directory, its own small local
state directory, and the Docker socket. Its Docker socket access is a host
administration capability. Bind-mount source paths in service manifests refer
to the Docker host, not paths inside the supervisor. On macOS, the Docker host is
the Linux VM; host filesystem sharing must already make the paths available.

The supervisor uses the v1.24 Engine API, compatible with the observed Pi
17.06 and Mac 24.x versions. Actual image/kernel/architecture compatibility must
still be qualified on each host. Services have Docker restart policy `no`;
only the grant-aware runner starts them. The supervisor itself may have
`unless-stopped`.

## Placement and failure behavior

A new supervisor incarnation stops only orphaned containers labeled as managed
by that same worker. It prepares images asynchronously, reports readiness, and
starts a service only when the coordinator returns its exact manifest grant.
Heartbeat responses cannot extend a lease by spending time in transit: local
deadlines start at the request's monotonic send time. An independent expiry
watchdog continues while image preparation or network reconciliation is delayed.

On expiry, revocation, manifest mismatch, or a changed owner token, the runner
kills and removes its managed child. It never stops unrelated containers. Its
current observations overwrite local SQLite rows, so there is no growing shared
execution journal. Docker logs rotate.

A gateway restart invalidates its old boot epoch and holds promotion through its
configured maximum lease interval. New grants have new tokens. Worker recovery
does not immediately evict a healthy successor. Explicit release supports
controlled handback.

A watchdog is not an output fence. A paused or partitioned host may run old code
until its scheduler resumes. Therefore:

- `effect_policy: stateless` means duplicate execution is safe.
- `externally_fenced` requires a pre-start HTTPS `fence_hook`. It receives the
  exact service, manifest, worker incarnation, boot epoch and grant token. It must
  atomically reject prior owners at the resource that accepts effects and return
  `{fenced:true,boot_epoch,grant_token}`. The runner forwards that acknowledgement
  and waits for the coordinator's exact effect_ready acknowledgement in the
  starting/active phase before launching the process. Application health then
  changes starting to active.
- `gateway_fenced` is reserved and currently rejected by the coordinator. There
  is no claim that existing application routes already enforce these grants.
- `state_policy: externally_managed` requires a qualified portable state backend.
  This runtime does not replicate arbitrary local directories or databases.

An unknown or unacknowledged fence keeps the service stopped. An existing
capability invocation is never replayed under a new controller automatically.

## Relay adoption

The six-channel ESP32 is itself a Wi-Fi provider. The current Pi is its authority,
not a USB serial relay bridge. Containerizing a Rover client cannot transfer
relay trust, resource generations, or custody. See
`docs/relay-authority-portability.md` for the required authority migration and
hardware qualification. Until those prerequisites are met, preserve the running
native Rover bridge and keep a relay manifest unregistered.

Future services need a pinned image per eligible architecture, a complete
manifest, health/readiness criteria, and an explicit state/effect policy. No
service-specific scheduler code is required for an already supported policy.

## Adding a future service

Initial manifest files seed the supervisor's local SQLite catalog. The gateway
returns admitted manifests to every worker, so a newly declared service is
learned, persisted and prepared by alternate eligible hosts automatically. A
worker keeps that catalog across gateway restarts. Updating an existing service
requires the coordinator's explicit revision-checked configure operation;
its configured revision is then distributed and old grants are drained. Merely
editing a different replica's seed file does not overwrite the active revision.
Conflicting revisions learned during gateway recovery block that service until
explicit resolution. Deletion/tombstone propagation is not implemented in this
version; stop and remove a declaration explicitly on all copies.

The ephemeral gateway cannot prove whether an offline replica has a newer
manifest. Qualification therefore records the prepared revision on every intended
standby. Do not label an out-of-date standby ready for a release it has not seen.

## Operator commands

Declare or update a service with `deploy/docker-services/manage.py` using the
private config directory created by the installer. A new service requires no
expected revision; an update requires the exact current revision. The gateway
distributes the admitted spec to the running supervisors.

```sh
python3 deploy/docker-services/manage.py --config-dir /path/to/private/config status
python3 deploy/docker-services/manage.py --config-dir /path/to/private/config declare service.json
python3 deploy/docker-services/manage.py --config-dir /path/to/private/config declare updated.json --expected-revision CURRENT_SHA256
```

Run `tests/qualify_docker_services.py --coordinator-source /path/to/service_coordinator.py --output /path/to/report.json`
with the coordinator dependencies and a built runner image. This uses two actual
Docker workers on one physical machine, pauses one, tests stale output rejection
at a test gate, verifies automatic manifest distribution, fails an active service,
and restarts the witness. It never qualifies physical Pi/Mac or relay behavior.
The report explicitly identifies those remaining hardware boundaries.
