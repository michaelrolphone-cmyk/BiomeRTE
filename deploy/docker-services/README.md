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
- `mounts` with absolute source/target and read_only, or named host-local bindings
  described below; legacy writable local binds must explicitly be disposable
  `cache_only`, never unreplicated authoritative state
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
  "request_timeout_s": 3,
  "docker_request_timeout_s": 30
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

The supervisor uses Engine API v1.24 on the Pi 17.06 and Mac 24.x engines. On
newer engines it reads the daemon’s minimum API version from `/version`. Actual image/kernel/architecture compatibility must
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
Docker create/start and kill/delete run in separate jobs, outside the grant lock.
Their transport budget is `docker_request_timeout_s` (default30 seconds; configure
60 seconds for the measured slow Pi). The heartbeat interval and grant expiry do
not grow with that budget. Fast Docker inspection requests retain a four-second
budget. Docker startup rechecks the full grant before create, before start, and
after completion; a renewed matching grant may survive a long start.

The runner records the child name before Docker create and the immutable container
ID before start. Release or expiry immediately withdraws local admission. The
runner verifies ownership labels and removes that exact ID. Cleanup failures keep
the current child reference in SQLite for retry after a daemon or supervisor restart.
If create loses its reply, the runner waits for the outstanding start job to finish.
An absent name is checked again after 15 seconds. An unanswered create has no ID
to start. The inventory sweep removes late, untracked children even after a new
lease is admitted. Raw Docker names and images never authorize fleet-wide cleanup.

### Disposable capability workers

For an exclusive capacity-one offer, set `state_policy` to `lease_scoped` and
supply `capability_lease` with the controller and provider fingerprints, resource
and versioned contract. Supply an argv health command that checks worker readiness.
The coordinator observes the matching controller lease without renewing it. A
fencing or bound lease admits the worker. Its container receives
`BIOME_CAPABILITY_LEASE_ID` and `BIOME_CAPABILITY_LEASE_GENERATION`. Retries
with the same binding reuse the child. A released lease, a replacement generation,
or an expired placement loses admission. The runner removes the child and its
anonymous volumes. Bind mounts and explicitly named volumes retain their contents.
Current transitions are stored in the local services table. Docker logs rotate.

A gateway restart invalidates its old boot epoch and holds promotion through its
configured maximum lease interval. New grants have new tokens. Worker recovery
does not immediately evict a healthy successor. Explicit release supports
controlled handback.

A watchdog is not an output fence. A paused or partitioned host may run old code
until its scheduler resumes. Therefore:

- `effect_policy: stateless` means duplicate execution is safe.
- `externally_fenced` requires an HTTPS `fence_hook`. Without staged activation,
  this hook runs before process startup. It receives the
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

An unknown or unacknowledged fence prevents active service admission. A staged
adapter may have an inert child running solely for preparation and fence traffic. An existing
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

## Stateful preparation and host-local bindings

A portable authority opts into `activation` with the exact protocol below.
This is a contract with an application adapter, not a flag that makes arbitrary
SQLite databases portable. The independently available adapter receives
checkpoints on every intended standby before the owner is lost. It verifies
checkpoint freshness and complete invocation custody before answering prepare.
A state directory cannot be relabeled as disposable cache to bypass this check.

A shared manifest declares logical bindings. Each worker resolves them using
its own private configuration, so different Mac and Pi paths do not alter the
manifest revision:

```json
{
  "activation": {
    "protocol": "biomerte-service-adapter/1",
    "binding": "relay-authority"
  },
  "effect_policy": "externally_fenced",
  "state_policy": "externally_managed",
  "mounts": [
    {"binding": "relay-private", "target": "/config", "role": "private", "read_only": true},
    {"binding": "relay-state", "target": "/state", "role": "state", "read_only": false}
  ]
}
```

These fields are added to a complete ordinary manifest, including its pinned
images, fence hook, and computed revision. The matching runner configuration
contains:

```json
{
  "bindings": {
    "relay-private": {
      "source": "/absolute/host/private",
      "role": "private",
      "service_id": "relay-authority"
    },
    "relay-state": {
      "source": "/absolute/host/state",
      "role": "state",
      "service_id": "relay-authority"
    }
  },
  "adapters": {
    "relay-authority": {
      "service_id": "relay-authority",
      "url": "https://local-adapter.example/control",
      "token_file": "/config/adapter.key",
      "timeout_s": 3
    }
  }
}
```

The installer accepts these private settings with `--host-settings PATH` and
supports `--docker-request-timeout-s 60` for a slow engine. It permits only
`bindings`, `adapters`, and `port_overrides` in that file; worker and coordinator
identity still come from the existing controller configuration.

The binding role and service identity must match exactly. Supported roles are
`config`, `private`, `state`, and `cache`. Config and private bindings are
read-only. Writable authoritative state requires this staged protocol and
`externally_managed`; it must never use `cache_only`. The source is an absolute
Docker-host path. The adapter must verify that the mounted state is the expected
state for the service; path syntax alone does not establish that.

Adapter transport defaults to HTTPS. A per-adapter `allow_local_http: true`
allows loopback/private literal IP addresses and Docker's host/gateway internal
names. Use this only on the intended protected local network. Credential files
are read inside the runner, and secrets never belong in the manifest. The
adapter is a separate, always-available host service; killing a placement child
must not kill the standby checkpoint receiver.

### Exact grant and checkpoint protocol

Let B be the JSON object with exactly these fields:

```json
{
  "service_id": "...",
  "manifest_revision": "<64 lowercase hex>",
  "worker_id": "...",
  "worker_incarnation": "...",
  "boot_epoch": "...",
  "grant_token": "..."
}
```

The runner POSTs requests to the adapter's configured URL. The id is SHA-256 of
canonical JSON `[protocol, operation, B]` (sorted object keys, compact separators).
A retry uses the same id, including after a lost response. The adapter must make
each operation idempotent and revalidate current authority before returning an
earlier outcome.

Prepare request:

```json
{
  "protocol": "biomerte-service-adapter/1",
  "operation": "prepare",
  "id": "<stable request digest>",
  "binding": "<B object>"
}
```

Prepare reply:

```json
{
  "protocol": "biomerte-service-adapter/1",
  "id": "<same request digest>",
  "binding": "<same B object>",
  "phase": "prepared",
  "state": {
    "checkpoint": "<64 lowercase hex>",
    "fresh": true,
    "custody_complete": true,
    "ready": true
  }
}
```

The literal placeholders above stand for objects and strings, not alternate
wire encodings. Missing, stale, unavailable, malformed, or incomplete custody
proofs fail closed. Uncertain prior operations must remain recorded as uncertain;
preparation must not promote them to successful or automatically replay them.

The runner then follows this order:

1. Cache the proof only while all six grant fields, manifest revision, and local
   monotonic deadline still match.
2. Wait for the coordinator to return `state_ready:true` for this exact grant.
   This acknowledges the reported baseline checkpoint before fence traffic can
   advance the service checkpoint head. A proof that finishes during a heartbeat
   waits for its next acknowledged report.
3. Create one inert child with `BIOME_SERVICE_PHASE=preparing`,
   `BIOME_SERVICE_CHECKPOINT`, `BIOME_SERVICE_ADAPTER_URL` from the host adapter
   configuration, and the existing `BIOME_SERVICE_*` grant variables.
   Ordinary process health does not advertise an active service in this phase.
4. Call the fence hook with B's six fields at the top level plus
   `prepared_state` set to the flat proof below.
5. Require `{fenced:true,boot_epoch,grant_token,binding:B,checkpoint}` with the
   same checkpoint. Report it to the coordinator.
6. Wait for the exact coordinator grant to have `effect_ready:true` and state
   `starting` or `active`, then request activation.
7. After an exact activation reply, sample the Docker daemon's clock and require
   a completed health probe that started after that boundary before advertising
   child health. Stale healthy or unhealthy results, including probes that span
   activation, remain unknown within the existing `startup_grace_s` (default 20).
   Missing or malformed samples fail closed when that fixed grace expires;
   fresh unhealthy results and stopped children fail immediately. A child with
   no configured or inherited health check retains ordinary running readiness.
   The coordinator's startup deadline and the exact grant/fence checks remain
   unchanged; a changed grant or child discards the health boundary.

The heartbeat proof is one exact shape:

```json
{
  "state_ready": true,
  "state_proof": {
    "protocol": "biomerte-service-adapter/1",
    "binding": "<B object>",
    "checkpoint": "<same checkpoint>",
    "fresh": true,
    "custody_complete": true,
    "ready": true
  }
}
```

The coordinator may select a provisional owner before this B-bound proof exists,
but must require the proof and matching fence acknowledgment before admitting
effects. Its preparation validation endpoint admits preparation/fence traffic
only; normal validation remains strict.

Activate request uses the same request envelope with `operation:"activate"`,
plus `checkpoint` and the exact `fence_ack`. Its reply repeats protocol, id, and
binding, with `phase:"active"` and the same checkpoint. A lost activation reply
causes a retry with the same id, never a second child or a replay of outputs.

The service adapter must enforce the preparing/active boundary inside the
application and validate authority independently. Environment variables and
runner health alone cannot make a stateful process inert. Preparation and fence
endpoints must not admit normal relay or application effects. Expiry, changed
grant, changed witness boot, changed manifest, and supervisor restart clear
prepared/activation proofs and stop managed children. A late network or Docker
reply is rejected. The independent watchdog remains active while adapter calls
are blocked.

Run the deterministic failure-boundary suite with:

```sh
python3 -m unittest tests.test_docker_services tests.test_service_adapters tests.test_docker_service_jobs -v
```

These tests qualify the generic protocol and lifecycle. Actual checkpoint
replication, sink fencing, authority startup, and Pi/Mac network failover still
require service-specific integration and hardware qualification.

## Private registry pulls

The supervisor talks directly to the Docker Engine socket. A login in the Mac
Docker CLI/keychain does not supply credentials to this Linux process. Configure
an optional absolute path in its private runner.json:

    "registry_auth_file": "/registry-auth/config.json"

Bind a dedicated host credential DIRECTORY read-only at /registry-auth.
Use directory mode 0700 and a regular config.json file with mode 0600 (0400 is
also accepted). A directory bind lets an atomic host-side replacement become
visible on the next pull; a single-file bind can retain the old inode.

The file uses a minimal Docker auth-file structure:

    {
      "auths": {
        "ghcr.io": {
          "auth": "<base64 of username:read-only-package-token>"
        }
      }
    }

An entry with exactly username and password is also accepted. Base64 is
encoding, not encryption. Keep the file in private host configuration, outside
repositories, images, release receipts, manifests and service mounts. The
registry key is an exact lowercase hostname with an optional explicit port.
A ghcr.io credential is never sent to ghcr.io.evil, ghcr.io:443 or another
registry. Mac keychain helpers and Docker credsStore/credHelpers are not
executed by the supervisor; do not mount the Mac's general Docker config.

The runner reads and validates the file at each pull, refuses symlinks,
nonregular files and group/world-accessible permissions, and sends the
URL-safe X-Registry-Auth header only to the Engine image-pull request. Missing
or malformed configured credentials fail closed before any pull. Registries
absent from a valid auths map continue to use anonymous pulls. With no
registry_auth_file configured, existing anonymous/local-image behavior is
unchanged. Cached current/previous images remain available without a new pull.

For GHCR, persistent hosts need a classic PAT with read:packages and access to
the intended private packages. Do not copy a short-lived Actions GITHUB_TOKEN
onto a host. Adding registry credentials does not replace the active supervisor,
change a service manifest or activate another workload; use the reviewed
ownership-draining rollout procedure when installing a new supervisor image.
