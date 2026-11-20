# Managed controller live qualification

This is an execution protocol, not a record of a successful deployment. Record actual UTC observations, exact source/image/config revisions and request identities as each step completes. Keep the existing development task in Rhizome and the deployment context/evidence in Humus. Follow the applicable established BPMN procedure for deployment, interruption, resumption and completion.

The subject is the existing site controller and its original paused responsibility, currently identified as `biome-proactive-assistant`. Confirm that ID against the reviewed source backup. Preserve its desired state, pinned process revisions, ownership epoch, active cycle and related run identities. Qualification does not require resuming the responsibility.

## 1. Required implementation set

The combined release must contain all of these components:

- Core controller authority, source custody/write fences, native assistant command binding, event canonical records, alias cursor lineage, stream aliases and independent local maintenance.
- Incremental controller roots and content-addressed chunk storage, transfer, complete-root verification and restoration.
- Companion controller routes, old-alias stream inspection, durable checkpoint witness and definite-refusal fencing.
- Site managed routing, private operator signer, event authorization and the native pairing input adapter.
- Source migration, sidecar export, source freeze, bootstrap generation and replica verification utilities.

Unit/integration test results and live hardware results are separate evidence. A passing synthetic transport test cannot establish that the Pi holds the latest production checkpoint or that a physical provider completed an operation.

## 2. Exact identities and configuration

Use one fresh logical controller identity. The Pi and Mac retain their own native identities, private keys and local realms. The logical identity's private material is explicitly installed on the two eligible managed replicas. Preserve the native public identities in migration provenance.

The following names are placeholders for the measured deployment values:

| Name | Meaning |
| --- | --- |
| SITE | Existing site ID |
| SERVICE | New controller service ID |
| AUTHORITY | Fresh logical controller fingerprint |
| PI_ALIAS, MAC_ALIAS | Original native controller fingerprints |
| PI_WORKER, MAC_WORKER | Stable existing Docker supervisor IDs |
| PROXY | Dedicated Site operator signing fingerprint |

### Helper and authority service.json

Each host receives the same logical scope and credentials, with its own worker ID and host-local paths. The same configuration file is used by that host's helper and placement child.

```json
{
  "authority_kind": "controller",
  "service_id": "SERVICE",
  "authority_id": "AUTHORITY",
  "site": "SITE",
  "controller_aliases": ["PI_ALIAS", "MAC_ALIAS"],
  "operator_actors": ["PROXY"],
  "operator_actor_aliases": {"PROXY": "admin"},
  "replica_workers": ["PI_WORKER", "MAC_WORKER"],
  "worker_id": "THIS_HOST_WORKER",
  "replication_policy": "available-peers",
  "event_default_controller": "PI_ALIAS",
  "coordinator_origin": "https://COMPANION_ORIGIN",
  "coordinator_key_file": "/config/coordinator.key",
  "checkpoint_key_file": "/config/checkpoint.key",
  "helper_key_file": "/config/helper.key",
  "checkpoint_db": "/state/checkpoints.sqlite",
  "authority_home": "/state/authority",
  "private_dir": "/config/identity",
  "adapter_url": "https://biomerte-controller-state:8870/adapter",
  "adapter_ca_file": "/config/helper-ca.pem",
  "helper_tls_cert_file": "/config/helper.crt",
  "helper_tls_key_file": "/config/helper-tls.key",
  "child_url_template": "http://{container_name}:8871",
  "helper_port": 8870,
  "authority_port": 8871,
  "event_authorize_url": "https://SITE_ORIGIN/internal/controller-events/authorize",
  "event_authorize_key_file": "/config/controller-event-auth.key",
  "request_timeout_s": 15,
  "maintenance_interval_s": 1
}
```

`child_url_template` is the child base URL; the helper appends `/fence`. `private_dir` must be the actual mounted directory holding `node.json` and `node.key` (`/config` if those files are directly in the private mount). The example uses a separate `/config/identity` subdirectory.

`event_site_access_file` is an optional private JSON file containing `token` when Site platform access requires that extra header. Event authorization still requires its own shared secret. The configured proxy must already be an enabled peer with exactly the intended fingerprint; its explicit operator mapping preserves historical native `admin` receipt identities. Other peers keep native authorization and are never inferred to be operators from a broad peer scope.

The current controller scope digest binds authority kind, service, logical identity, site, sorted controller aliases, explicit operator actors/mappings, replication policy and event default alias. Generate bootstrap state using the same release and exact scope configuration that the helpers will load. Do not compute the bootstrap digest with the old physical-service scope function.

The private logical `node.json` can contain:

```json
{
  "stream_authorities": {
    "PI_ALIAS": {"secret": "PRIVATE_OLD_PI_STREAM_HMAC", "url": "wss://EXISTING_STREAM_ORIGIN"},
    "MAC_ALIAS": {"secret": "PRIVATE_OLD_MAC_STREAM_HMAC", "url": "wss://EXISTING_STREAM_ORIGIN"}
  }
}
```

These entries supplement the normal generated logical node configuration. Populate them through private credential transport. They preserve old stream-grant authorities and HMAC verification; native node RSA keys and native admin tokens remain on their original hosts.

### Shared service manifest and supervisor bindings

The authority declaration is:

```json
{
  "kind": "controller",
  "id": "AUTHORITY",
  "controller_aliases": ["PI_ALIAS", "MAC_ALIAS"],
  "resources": [],
  "providers": []
}
```

Use this as `manifest.authority`. The companion must already reserve both native aliases in its configured native authorities. The logical ID must differ from them.

The complete manifest also requires immutable images for the actual `linux/arm/v7` Pi and `linux/arm64` Mac platforms, `effect_policy:"externally_fenced"`, `state_policy:"externally_managed"`, and:

```json
{
  "activation": {
    "protocol": "biomerte-service-adapter/1",
    "binding": "SERVICE"
  },
  "command": ["--role", "authority", "--config", "/config/service.json"],
  "mounts": [
    {"binding": "controller-private", "target": "/config", "role": "private", "read_only": true},
    {"binding": "controller-state", "target": "/state", "role": "state", "read_only": false}
  ],
  "network": "biomerte-services",
  "fence_hook": {
    "url": "https://biomerte-controller-state:8870/fence",
    "token_file": "/config/controller-helper.key",
    "timeout_s": 20
  }
}
```

The fence-hook token path is inside the supervisor; it names the same credential whose path is `helper_key_file` inside the helper/child. Configure the host-specific supervisor bindings and adapter entry accordingly. Keep both replicas on exactly the same admitted manifest revision. Compute `revision` with `biomerte.docker_services.revision(manifest)` after every manifest field is final.

The controller fence acknowledges logical write ownership only. It does not increment every physical lease generation, revoke unrelated leases, or prove that a stopped physical device is quiescent.

### Site routing

Set:

| Site setting | Value |
| --- | --- |
| CONTROLLER_RUNTIME_REQUIRED | `1` |
| CONTROLLER_RUNTIME_AUTHORITY | JSON declaration below |
| CONTROLLER_RUNTIME_SIGNING_KEY | Private PKCS8 PEM for PROXY |
| SERVICE_AUTHORITY_ORIGIN | Companion HTTPS origin, with no path/query/userinfo |
| CONTROLLER_EVENT_AUTH_SECRET | Separate private event authorization secret |
| DURABLE_WITNESS_SECRET | Separate private witness secret |
| DURABLE_WITNESS_ID | `biomerte-services`, or the same explicit configured witness ID on both sides |

```json
{
  "service_id": "SERVICE",
  "authority": "AUTHORITY",
  "controller_aliases": ["PI_ALIAS", "MAC_ALIAS"],
  "operator_actor": "PROXY",
  "event_default_controller": "PI_ALIAS"
}
```

The retained Site custody declaration prevents removing environment flags from silently restoring the old native site writer route. A failed managed call must stay an explicit failure and permit an identical-ID retry.

### Companion witness

Configure the companion through either explicit witness environment settings or the Git-pinned public `durable-witness.json` deployment declaration:

```json
{
  "format": "biomerte-durable-witness-deployment/1",
  "origin": "https://SITE_ORIGIN",
  "witness_id": "biomerte-services",
  "lease_s": 120
}
```

The declaration accepts exactly these four public fields and a canonical bare HTTPS origin. A present malformed file or conflicting explicit origin/ID/lease setting stops startup. With that file, the companion derives a separate witness credential from its existing coordinator bearer using the domain-separated `biomerte/durable-witness/deployment/v1` context, origin and witness ID. Provision the matching Site secret privately before the companion rollout. Neither the parent credential nor the derived secret belongs in the file.

The environment-only path requires `DURABLE_WITNESS_ORIGIN` and an explicit `DURABLE_WITNESS_SECRET`; `DURABLE_WITNESS_ID` and `DURABLE_WITNESS_LEASE_S` are optional. An explicit separate secret can override derivation. It must differ from both the stream-router secret and the coordinator bearer. Removing both a deployment file and all witness settings is legacy mode, so release review must retain the intended deployment declaration.

An existing ephemeral service head needs separately proven enrollment before a companion restart. The old coordinator's `release` operation is not a durable pause; a later heartbeat may make that worker eligible again. Do not seed a new D1 reference from a lone helper or from one transient no-grant observation.

The witness lease must cover at least twice the coordinator's configured maximum grant TTL. New epochs use server-time lease exclusion plus quarantine covering the greater of the prior and new maximum grant intervals. Actual service grants are capped by the client's conservative monotonic witness deadline.

D1 stores one current witness lease and one latest sequence/digest/scope reference per service. Runtime records, artifacts and checkpoint chunks remain durable on the hosts. This metadata table is not an execution journal.


### Native Python 3.4 boundary

The physical native controller/gateway remains compatible with its Python 3.4 interpreter. Modern controller authority, checkpoint and daemon modules run only inside the pinned Python 3.11 service image. Application and native gateway imports must not eagerly load those modern modules.

The isolated compatibility suite checks the changed native source grammar and blocks accidental managed-module imports. Both deployment event modules remain self-contained so a standalone gateway bundle does not need the core package during its initial imports.

When the Pi is reachable, test the candidate release with its actual interpreter before changing live services:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/absolute/candidate-release /usr/bin/python3 -c 'import sys; from biomerte.app import Application; import biomerte.web; assert sys.version_info[:2] == (3, 4)'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/absolute/candidate-release/deploy /usr/bin/python3 -c 'import mcp_events; import grok_receivers'
```

These are source/import smoke checks, with no Application construction or runtime mutation. Record the actual interpreter/version and results. A Mac grammar test or Docker Python 3.11 result does not substitute for this native observation.

## 3. Source custody and bootstrap

1. Record both native source identities, current software revisions, stopped/running service inventory, canonical site digests and the three event sidecar table inventories. Obtain an independent Mac and Pi maintenance path before changing site custody.
2. Keep the original responsibility paused. Record `assistant.status {"id":"biome-proactive-assistant"}`, plus complete canonical baseline rows for its responsibility, assistant config, cycle/checkpoint/outbox/episode state, pinned process revisions, referenced work/runs, leases/actions and relevant receipts. Store digest evidence without printing private keys or callback secrets.
3. Stop the old native site writers and old gateway event delivery processes for the final source backup. A new marker cannot change a previously running unpatched binary. Keep independent host maintenance available.
4. Complete the reviewed migration plan, explicit record-conflict resolutions, source event lineage and receipt conflict markers. Unresolved active ownership/session protocols stop the migration. Preserve source public keys and every displaced original in provenance.
5. Install each exact source marker with `deploy/controller-authority/deploy.py freeze`. Its transaction verifies the reviewed source digest and installs custody while holding the source SQLite write lock. Separately prove patched native site calls reject writes and effect-bearing reads; the marker receipt alone is not runtime proof.
6. Build the bootstrap with the current migration utility:

```sh
python3 deploy/controller-authority/migrate.py bootstrap \
  --source SOURCE_EXPORT_A --source SOURCE_EXPORT_B \
  --plan plan.json --marker pi-marker.json --marker mac-marker.json \
  --config staging-helper.json --output bootstrap-packet.json
```

Each repeated `--source` names a reviewed JSON source snapshot, a WAL-safe backup manifest beside its SQLite payload, or a verified detached source-chunk manifest. The command requires a fresh checkpoint database. It writes the signed root and content-addressed blocks in `checkpoint_db + ".chunks"`; it does not activate a service.

7. Privately install the exact bootstrap checkpoint database/root and complete block set for each helper. Supplying only the small root JSON is insufficient. With a new empty checkpoint database, `bootstrap_packet` can install the root only after its referenced blocks are present in that database's chunk directory.
8. On each host, run:

```sh
python3 deploy/controller-authority/deploy.py verify-replica \
  --config HOST_HELPER_CONFIG --expected bootstrap-packet.json \
  --receipt HOST_VERIFICATION_RECEIPT
```

Require matching exact reference, signature, logical identity, complete block/shard verification, canonical row counts and scope. Record both independent verification receipts.

## 4. Startup and first admission

Run the helper independently of any placement child, on the protected service network with private read-only configuration and durable state mounts:

```sh
python -m biomerte.service_daemon --role helper --config /config/service.json
```

The helper may use Docker `unless-stopped`. The placement child uses restart policy `no` and is created only by the grant-aware supervisor:

```sh
python -m biomerte.service_daemon --role authority --config /config/service.json
```

Do not manually synthesize its grant environment. The supervisor supplies all six binding fields through `BIOME_SERVICE_ID`, `BIOME_SERVICE_REVISION`, `BIOME_SERVICE_WORKER`, `BIOME_SERVICE_INCARNATION`, `BIOME_SERVICE_BOOT_EPOCH` and `BIOME_SERVICE_GRANT`, plus the prepared checkpoint and adapter URL.

Declare the complete service only when the reviewed source freeze and bootstrap copies are established. Require this sequence in observed evidence: exact state preparation, witnessed baseline, child preparation, logical fence proof, coordinator effect admission, then active health. During preparation, ordinary site calls and callbacks must fail closed.

The authenticated coordinator APIs are:

| API | Request / evidence |
| --- | --- |
| GET /api/service-coordinator/status | Current epoch, witness readiness, admitted manifest, candidate incarnations, owner and remaining TTL |
| POST /api/service-coordinator/validate | Exact six-field active binding |
| POST /api/service-coordinator/validate-preparation | Exact six-field preparation binding |
| POST /api/service-checkpoints/status | `{"service_id":"SERVICE","workers":[]}`; returns current head and authenticated helper observations |
| POST /api/controller-runtimes/AUTHORITY/call | Exact original signed native site envelope |
| POST /api/controller-runtimes/AUTHORITY/pair/begin | `{"public_key":"...","label":"..."}`, pending candidate only |
| POST /api/controller-runtimes/AUTHORITY/pair/finish | Internal `{"proof":{"key":"...","signature":"..."}}` |
| POST /internal/controller-events/authorize | Dedicated-secret request `{"owner":"...","grant":null}` or an exact grant ID |
| POST /internal/service-witness/v1 | Dedicated-secret metadata protocol; record only sanitized reference/epoch evidence |

The Site's public native provider pairing finish remains the existing flat challenge/site/key/signature shape; its managed wrapper adapts to the internal proof shape. Successful native pairing replies retain `{"result":...}`. Approval remains a separately authenticated native operation.

## 5. Normal-route and receipt proof

Through the ordinary Site tool path, address PI_ALIAS, MAC_ALIAS and AUTHORITY in turn. Require the same logical `system.info.node`, `cluster.status.mode:"managed-controller"`, configured aliases and original paused responsibility. Capture the actual current service binding and checkpoint references separately; `cluster.status` is descriptive, not proof of a current grant.

Use one harmless knowledge qualification document with a unique key and stable request ID, for example:

```json
{
  "operation": "knowledge.put",
  "arguments": {
    "key": "qualification.controller.QUALIFICATION_ID",
    "body": {"qualification": "QUALIFICATION_ID", "stage": "before-owner-loss"},
    "parents": []
  },
  "request_id": "qualification-QUALIFICATION_ID-write"
}
```

Save the returned revision and its exact native receipt identity. Repeat the identical call and require the same revision/result. A reused ID with different arguments must fail without a new revision. This deliberately leaves a small qualification evidence document; it does not create or resume autonomous development work.

After an acknowledged mutation, require the witness head and every healthy advertised helper to agree on the exact sequence/digest. A root acknowledgement counts only when the helper verified all referenced chunks. Record the helper's complete/degraded status; never label a missing standby as current.

Verify scoped native provider envelopes continue through the managed route with their original actor and original signed controller alias. A tampered signature, disabled peer or out-of-scope operation must fail. Verify the source native site's `provider.poll` is fenced while the source's local-realm maintenance still works.

## 6. Owner loss and survivor

Record the active owner, full binding, acknowledged reference and exact source of the outage. Remove the active controller service's execution/connection using the established outage procedure. Keep the unrelated relay service and host-local maintenance outside this test.

Require the old grant to become invalid before the successor becomes active. Record actual timings against configured grant/worker/witness intervals; do not use a guessed fixed sleep as evidence.

On the survivor:

- Require restoration of the exact witnessed state, complete chunks, new binding and active admission.
- Read the existing assistant and verify paused desired state, original ID, pinned revisions, ownership epoch, cycle/run identities and preserved pending/uncertain operation IDs against baseline.
- Retry the exact knowledge mutation with the original request ID. Require the identical revision and receipt outcome, with no second writer revision for that call.
- Require old binding validation to return invalid and any late old-process reply to be rejected.
- Publish a second small qualification revision using the first revision as parent and a fresh request ID. Verify the updated witnessed reference remains available while the former owner is absent.

If the first owner was already absent before installation, this establishes survivor startup and degraded operation only. Full automatic two-host failover still requires recovery of the missing host and an observed ownership transition.

A preserved offline outcome addressed to an original native alias is accepted only by the configured managed site realm. Its original signature, enabled peer/return scope and exact grant binding remain mandatory. Local realms and unconfigured sources retain their original destination rule; outbound packages use the new logical identity and require the receiver's explicit trust in that public key.

Physical provider custody remains physical. Preserve worker-ledger action/result/ACK identities and existing resource generations. An unreported effect remains uncertain until that provider reconciles; controller availability alone is not evidence that the physical effect completed.

## 7. Returning stale host and witness restart

Return the former owner with its old local checkpoint. Require it to fetch and verify the current root's missing chunks before it can acknowledge that root. It must remain standby while the successor is healthy. The old grant and old process remain inadmissible. Verify exact helper/witness reference agreement after catch-up.

With one host then deliberately absent and the survivor holding the witnessed current root, restart the companion witness. Record:

1. No grant while the previous durable witness lease remains owned.
2. A new epoch after lease exclusion, followed by the prescribed prior-grant quarantine.
3. Loading the exact D1 reference without accepting a worker's lower reference as authoritative.
4. Survivor preparation from that exact root, a new binding and successful same-ID retry.
5. Rejection of the old epoch's delayed CAS and old service binding.

Unknown CAS outcomes must hold the publication-reconciliation barrier until the exact CAS is reconciled or a newer durable epoch fences it. A definite lease or commit epoch refusal must immediately remove cached authority. Test lost-response behavior in the isolated transport suite; avoid creating an ambiguous production effect solely to exercise that fault.

## 8. Event and stream continuity

For each original alias with an actual site event registration, compare subscription ID, producer alias, key fingerprints, cursor, retry state, generation and receiver receipt identities against the source baseline. Never print callback URLs containing credentials, keys or bearer tokens in qualification output.

Use an already authorized controlled callback fixture to prove delivery continuity. The same stable event ID may repeat if the remote receiver accepted a callback before its cursor commit survived; the receiver must deduplicate it. A delegation recipient still claims the same canonical delegation before acting. No claim of exactly-once HTTP delivery is made.

Require only the granted controller daemon to send callbacks. Old source gateway agents must skip site delivery after the marker. Local-realm subscriptions remain on their physical host.

Inspect an existing stream grant under its original native authority alias. Require the same grant ID, lease, resource generation and alias; the companion must route inspection to the active logical service while validating the original authority. A wrong alias or stale grant must fail. Stream liveness on a disconnected physical provider is a separate result.

## 9. Completion evidence

Retain a concise report with source/image/config revisions; migration and source-freeze receipts; both replica verifications; original responsibility and operation digests; normal-route results; before/after binding and reference values; acknowledged/degraded replication status; measured failover and witness restart timing; callback/stream evidence where applicable; and explicit unobserved physical boundaries.

Do not mark two-host durability or automatic takeover complete while the Pi is inaccessible or its production bootstrap/current reference has not been independently verified. Preserve the original paused state unless a later authorized workflow explicitly resumes it.

### Native recovery boundary before source fencing

The original source guard is insufficient if an independent watchdog or
branch watcher can subsequently execute an old unpatched release. Before
installing a source marker, qualify the stable `controller_custody.py`,
`controller_autodeploy.py`, `controller_autodeploy_exec.py`,
`controller_autodeploy_service.py` and `controller_watchdog.py` copies on
each actual applicable service path. Copy the shared helper beside every
installed recovery program, including the watchdog's private directory.
Configure the watchdog's `service_configs` for the exact native and link
launcher configurations. A Pi that does not run autodeploy must still enter
the guarded stable exec boundary from its actual native and gateway units.

Hold the existing shared recovery lock while the old writers/recovery actors
are quiesced and verify the stable installation, current physical release,
actual running source and marker installation. The independent exec child must
not reacquire its parent watcher's lock. The raw source backup and final
canonical digest still require all unpatched site/event writers stopped.

A compatible native source reports exact `cluster.status` frozen-source
identity. A healthy source is not required or allowed to run
`system.discovery`. Prove that site writes remain rejected and that an old
LKG cannot be imported, selected or executed by candidate, interrupted
rollback, watchdog, native restart helper, or direct stable-launcher paths.
An incompatible rollback must leave the current pointer unchanged and durable
`rollback_failed` evidence while permitting a subsequent compatible repair.
These gates supplement the authority/replica/witness qualification above;
they do not prove live failover or authorize an unobserved cutover.
