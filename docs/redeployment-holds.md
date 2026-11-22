# Redeployment holds

Redeployment holds let an operator replace firmware, restart or upgrade a service,
or reconfigure a registered resource while retaining its pending reservations.
The controller closes normal admission, parks undispatched work, and restores
the same reservation IDs after verified deployment or rollback.

This document describes the source contract in `biomerte/redeployment.py`.
It is not a record of a deployed firmware version, a completed rollout, or
physical-device qualification.

## Scope and authority

Use the controller and realm that own the resource's existing reservations.
Keep that authority explicit throughout begin, status, maintenance, verification,
and completion. A hold does not transfer lease ownership, an in-flight command,
or its completion route to another controller. Controller failover and ownership
transfer remain separate operations.

Choose exactly one target selector when beginning a hold:

| Selector | Meaning |
| --- | --- |
| `resource` | One registered stable resource ID. |
| `resources` | A nonempty list of registered stable resource IDs. |
| `provider` | All resources offered by that provider. |
| `service` | All resources offered under that service name. |

Use identities returned by capability discovery, not an IP address inferred from
a device name. Provider and service holds also cover matching resources registered
while the hold is active. Check the latest `resources` list before verification,
because every resource in that list needs completion evidence.

Redeployment control operations require operator access. The hold ID and
generation identify the deployment being controlled; they do not replace the
normal authority and authorization checks.

## Reservation and execution behavior

Beginning a hold preserves queued requests, idle bound reservations, shared
stream subscriptions, and undispatched capability operations. Their IDs remain
available. They are marked held rather than deleted and recreated.

Ordinary acquisition and dispatch cannot enter the held resource. A newly
requested reservation pinned to that resource is retained. Requests eligible for
an unaffected implementation can still use that implementation. Other resources
continue operating.

A command already claimed by a provider, or carrying an uncertain outcome, keeps
its original execution owner and generation until it settles. It remains a real
deployment blocker. Beginning a hold does not cancel a physical effect or treat
missing output as success. Pending configurable-resource activations are also
reported as blockers.

At completion, previously bound reservations are considered before waiting
requests, and existing queue order is retained. Rebinding may require a fresh
provider fence or wait for capacity, so completion does not mean every lease is
immediately bound. Use `cap.lease` and existing lease-availability handling.

Held reservation idle time is preserved. A hold has no automatic expiration or
automatic release deadline. Explicit action `deadline_at` values are not paused,
and held actions with an elapsed deadline or a kill request are cancelled instead
of dispatched. Time-sensitive device contracts must continue enforcing their own
freshness rules.

## Operations

These operation names are passed to `biome_invoke`; the arguments below refer to
its `arguments` object.

| Operation | Required arguments | Optional arguments |
| --- | --- | --- |
| `cap.redeploy.begin` | `id`; exactly one target selector | `reason`, `expected_version` |
| `cap.redeploy.status` | Exactly one of `id` or `resource` | None |
| `cap.redeploy.quiesce` | `id`, `generation`, `evidence` | None |
| `cap.redeploy.submit` | `id`, `generation`, `contract`, `inputs`, `operation` | `resource` |
| `cap.redeploy.prepare` | `id`, `generation`, `contract` | `resource` |
| `cap.redeploy.fail` | `id`, `generation`, `reason` | `evidence` |
| `cap.redeploy.retry` | `id`, `generation` | None |
| `cap.redeploy.retarget` | `id`, `generation`, `expected_version`, `reason` | None |
| `cap.redeploy.complete` | `id`, `generation`, `evidence` | `outcome`: `deployed` or `rolled_back` |

Use a unique hold `id` for a logical deployment and retain the returned
`generation`. A repeated begin with the same ID and identical selector, reason,
and expected version returns the same deployment. Reusing that ID with different
values conflicts. Overlapping active resource holds also conflict.

Reuse an MCP mutation `request_id` only for an identical retry. A transport
timeout does not establish that a call failed to apply; inspect the hold or
command status before deciding what to do next. Use a distinct `operation` ID
for each distinct maintenance command.

## Deployment procedure

### 1. Begin and retain the returned identity

Example envelope; replace all angle-bracket placeholders with discovered or
observed values:

```json
{
  "controller": "<owning-controller-id>",
  "realm": "site",
  "operation": "cap.redeploy.begin",
  "request_id": "deploy-front-camera-begin-20261118",
  "arguments": {
    "id": "deploy-front-camera-20261118",
    "resource": "<resource-id>",
    "reason": "Install the requested firmware version",
    "expected_version": "<requested-version>"
  }
}
```

`expected_version` is an exact string comparison used for a successful
`deployed` completion. Supply the representation that the returned firmware or
service actually reports. An omitted value disables this requested-version
comparison; observed nonempty version evidence is still required.

### 2. Read readiness and resolve actual blockers

Call `cap.redeploy.status` with the hold ID, on the same controller and realm.
Its response includes:

- `id`, `generation`, `selector`, and the current `resources`;
- `state`, `ready_to_deploy`, and `blockers`;
- `held_leases`, `held_operations`, and `held_streams`;
- failure or completion evidence when recorded.

`draining` means a blocker remains. `redeploying` with
`ready_to_deploy: true` means the reported drain conditions have been satisfied.
`failed` retains the hold and requires explicit recovery. `completed` is a
finished hold, not an active maintenance window.

Blocker kinds include executing/uncertain `operation` records, queued
`maintenance` work, pending `activation` records, and `stream` quiescence.
Read the exact IDs and states. Do not purge reservations or mark unresolved
effects complete to clear this list.

### 3. Quiesce existing streams at their enforcement point

Every existing open stream covered by the hold requires explicit quiescence
evidence, including a stream whose payload is read-only telemetry.

Holding a stream invalidates its transport authority and removes it from normal
provider stream admission. The router rejects new attachments and closes existing
peers on its next authoritative validation. This does not synchronously prove
that a device-owning adapter, camera capture, or physical control effect stopped.
The 120-second attachment credential lifetime is not a stream-drain timeout.
A worker's ordinary fence acknowledgment alone does not establish stream shutdown.

Stop the affected interaction through its actual provider or service management
path, then record the observation. `cap.redeploy.quiesce` records evidence; it
does not perform the stop operation for you.

Example arguments, using the generation returned by begin:

```json
{
  "id": "deploy-front-camera-20261118",
  "generation": 17,
  "evidence": {
    "streams": {
      "<held-stream-session-id>": {
        "quiescent": true,
        "method": "provider_stopped",
        "observation": {
          "provider": "<provider-id>",
          "service": "<service-name>",
          "state": "stopped",
          "source": "<actual-stop-or-inspection-result>"
        }
      }
    }
  }
}
```

The sample generation `17` is illustrative. Each proof must contain
`quiescent: true`, a nonempty `observation` object, and one of these methods:

- `provider_stopped`: observed shutdown of the component owning the interaction;
- `resource_fenced`: observed enforcement of the new authority at the actual
  device/resource boundary;
- `device_offline`: observed device state that establishes the affected
  interaction has stopped.

Supply only evidence that was actually observed. A lost connection, unreachable
health endpoint, requested stop, or elapsed timer is not by itself proof of
quiescence. Evidence is keyed by exact held stream session ID and checked against
the current hold.

Do not use `stream.close` to implement suspension: it releases the consumer's
reservation. The hold keeps shared reservations intact.

### 4. Perform the deployment while admission remains closed

Use the established firmware or service deployment procedure. If the maintenance
command itself needs an offered capability on a held target, use
`cap.redeploy.submit`:

```json
{
  "id": "deploy-front-camera-20261118",
  "generation": 17,
  "contract": "<maintenance-capability-contract>",
  "resource": "<resource-id>",
  "inputs": {},
  "operation": "deploy-front-camera-install-20261118"
}
```

Replace `inputs` with the actual capability contract's inputs. This is a scoped
maintenance submission; it does not reopen ordinary admission. The contract must
have an offer on a resource belonging to this hold. When multiple target
resources match, supply `resource` explicitly. Read the command through the
returned status guidance and `cap.status`; submission alone is not completion.

For a configurable replacement, use `cap.redeploy.prepare` with the hold ID,
generation, contract, and optional resource. This requests the existing
configuration/activation path while normal reservations remain held. Wait for the
provider's configuration result and inspect pending activation blockers.

Submit and prepare require drain readiness and reject failed or completed holds.
They are not bypasses for the stream quiescence step. If stopping a live adapter
must happen before drain readiness, use its established service-management or
enforcement-point shutdown path, then record the resulting observation.

### 4a. Revise the requested version during recovery

If the candidate needs a different firmware or service version, the operator who
owns the active hold can call `cap.redeploy.retarget` with its current ID and
generation, a nonempty `expected_version`, and a nonblank `reason`.

```json
{
  "id": "deploy-front-camera-20261118",
  "generation": 17,
  "expected_version": "<revised-requested-version>",
  "reason": "<observed reason this recovery needs a different version>"
}
```

Retarget changes only the expected version and appends an attributable
`version_history` entry containing the previous/new versions, reason, actor,
generation and observation time. It keeps the same generation, resource scope,
reservations, operations, streams, blockers and failure state. It performs no
deployment, health check, release, retry or reconciliation. A failed hold still
requires explicit `cap.redeploy.retry` before maintenance can resume.

Only the hold owner with operator access may retarget. Stale generations,
completed holds and blank versions/reasons are rejected. Repeating the same
version is a no-op, and normal request-ID idempotence applies. Use a fresh request
ID for a different version or reason. Read status to recover the current plan;
`begin` is not a version-edit operation.

After retarget, a `deployed` completion must report the new expected version
and actual healthy evidence for every resource. The old version cannot complete
that deployment. Retarget never makes an unhealthy candidate successful and
never substitutes for a verified rollback.

### 5. Verify the returned resource and complete

Obtain health and version information from the running resource after the change.
A build artifact, source commit, successful upload, or process launch alone does
not establish runtime health.

Call `cap.redeploy.complete` with evidence for every resource in the latest
hold snapshot:

```json
{
  "id": "deploy-front-camera-20261118",
  "generation": 17,
  "outcome": "deployed",
  "evidence": {
    "resources": {
      "<resource-id>": {
        "healthy": true,
        "version": "<observed-version>",
        "observation": {
          "identity": "<observed-resource-identity>",
          "source": "<actual-health-and-version-result>"
        }
      }
    }
  }
}
```

Completion requires no remaining blockers, `healthy: true` and a nonempty
version string for every target resource, and at least one registered ready
offer for each target. With `outcome: deployed`, the reported version must equal
`expected_version` when one was supplied.

The API validates the supplied evidence and current registration state; it does
not independently flash firmware, run a health probe, or verify arbitrary
observation fields. The caller must obtain truthful observations from the
resource and retain their provenance.

Successful completion releases the admission hold and resumes the same
reservation IDs. Shared stream subscriptions remain associated with their
preserved reservation group. Retained stream session IDs receive a fresh epoch
and current provider generation; old attachment credentials and payload epochs
must not be reused. Wait for lease binding, then request fresh attachment
credentials using the normal stream API. A stream whose reservation cannot be
restored is closed and can be opened again after its lease becomes usable.

Repeating completion with the identical outcome and evidence is idempotent.
Different completion evidence or outcome for an already completed hold conflicts.

## Failure, retry, and rollback

On failure or an uncertain deployment result, call `cap.redeploy.fail` with the
current ID/generation, a nonblank reason, and any observed evidence. Reservations
stay held. Normal callers cannot consume a partially deployed resource, and
maintenance submit/prepare are disabled while the hold is failed.

Use `cap.redeploy.retry` to resume that same deployment window after addressing
the failure. Retry retains the hold ID and generation and does not release
pending work. Re-read status before submitting another maintenance command.

For rollback, perform and verify the rollback, then call
`cap.redeploy.complete` with `outcome: rolled_back`. Every target still needs
healthy, nonempty-version evidence and a ready offer. The requested replacement
version comparison is skipped because the observed version is the rollback
version.

There is no automatic release when a caller disconnects, a controller restarts,
or a timer elapses. Finish with verified deployed/rolled-back completion. A
failed hold remains visible and recoverable instead of silently resuming work.

## Operational checks

- Preserve the returned hold identity and generation across reconnects.
- Keep requests directed to the original owning controller and realm.
- Check runtime health/version and all dynamically included resources.
- Distinguish held demand from an executing or uncertain effect.
- Record actual stream enforcement-point quiescence before deployment.
- After completion, check lease rebinding and refreshed stream credentials.
- Leave release/cancellation decisions to their existing APIs; do not delete
  reservations to make deployment proceed.
