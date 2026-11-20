# Managed site controller authority

A controller authority owns one complete site realm under the service coordinator's current grant. Physical host identities and their local realms remain independent. The same logical private controller identity and verified checkpoint may be restored on either configured worker. The source homes have a controller-authority.json custody marker; patched native APIs reject site writes and effect-bearing reads, including provider.poll and stream.inspect. Passive diagnostics remain available.

## Configuration and identities

The helper/authority service configuration uses:

- authority_kind: controller
- service_id, authority_id and site
- controller_aliases: the explicitly migrated native controller IDs
- operator_actors: the exact enrolled gateway proxy fingerprints
- operator_actor_aliases: optional explicit proxy fingerprint to admin mapping, preserving historical native operator receipt IDs
- replica_workers: stable helper worker identities
- replication_policy: available-peers
- event_default_controller: one configured original controller alias
- event_authorize_url: the private HTTPS Site endpoint that checks the current OAuth owner/grant
- event_authorize_key_file: private file containing the shared event authorization secret
- event_site_access_file: optional private Site access JSON containing token

Controller aliases are accepted without changing the signed envelope. Enrolled peers still require enabled credentials and native operation permissions. Only explicitly configured operator actors receive operator context. New receipts retain the authenticated proxy fingerprint even when the effective historical actor is admin.

The private logical node.json may additionally carry stream_authorities, mapping an original controller alias to its stream signing secret and public WSS URL. Persisted stream grants retain their original authority, lease, generation and identity. A legacy token is signed with that explicitly configured authority's stream secret. Native node RSA keys and native admin tokens are not cloned.

cluster.status distinguishes managed-controller placement from a permanent shared journal. Its placement binding is descriptive; the helper/coordinator validate the active grant and exact durable checkpoint before an effect-bearing reply.

## Durable state and snapshots

Every canonical record is part of the checkpoint, including future record kinds, assistant configuration and responsibilities, cycle/checkpoint/outbox state, work, runs, waits, receipts, events, artifacts, leases and pending/uncertain actions. Logical takeover does not increment physical resource generations or assistant responsibility epochs.

The controller maintains disposable SQLite dirty-key and hash-bucket indexes. After initial indexing, snapshot generation reads only changed kind/hash buckets. Artifacts do not get decoded or retransmitted because an unrelated provider heartbeat changes. The signed root references bounded immutable content-addressed blocks, and each receiving helper must durably possess all referenced blocks before acknowledging the root. No historical operation journal is replayed.

Healthy advertised replicas must acknowledge publication. An unavailable peer permits explicit degraded operation on the survivor; a returning stale peer cannot become owner by presenting an old snapshot. A response lost after local commit is retried with its original request/operation identity. A failed publication never authorizes a second execution.

## MCP and Grok event state

The managed runtime reuses the existing EventStore and Grok receiver semantics. Per-source SQL projection tables live beside canonical records, but they are disposable. SQLite triggers mirror each row change atomically into:

| Canonical kind | Preserved source table |
| --- | --- |
| _gateway_event_subscriptions | subscriptions, including keys, cursor, generation, expiry and retry state |
| _gateway_event_receipts | receiver_receipts |
| _gateway_event_failures | failed_events |

Each record includes source_controller and the complete original row. Its canonical key includes the source alias, so independent source registrations cannot overwrite each other. Projection tables are rebuilt after restoration; checkpointing never depends on an unreplicated auxiliary SQLite file.

Gateway calls use the normal exact signed native envelope:

- gateway.events.change with controller, method, params, owner and grant
- gateway.events.grok with controller, action, args, owner and grant

These operations require the enabled configured operator signing key. The Site derives owner/grant from authenticated access before signing. Untrusted caller-supplied owner or grant fields must not be forwarded as operator authority.

Source event counters were independent. Migration preserves each source's retained event rows and watermark in _controller_source_feeds, _controller_source_events and _controller_source_delegation_events. Each alias continues its old numeric cursor range, then projects new logical events above the migration barrier into that producer's continuing range. Event and delegation IDs remain unchanged.

Only the active daemon delivers callbacks. It publishes committed state and validates its grant before each send, then mirrors cursor/retry outcomes. The maintenance loop sends at most one callback per pass and rotates producers/subscriptions. Delivery is at least once: a crash after a remote HTTP acknowledgement but before its durable cursor update can repeat the same stable event ID. Receivers must retain their existing event-id deduplication; delegated workers must claim the original native delegation before acting.

Patched source gateway-agent processes reject site event mutations and skip site delivery when the custody marker exists. Their local-realm subscriptions remain local. Running old binaries must be stopped before the final source event snapshot and restarted only after installing the patch/marker.

## Physical execution and external protocols

Provider worker.sqlite, terminal command receipts and operating-system process custody remain bound to their physical provider. Reported outcomes and uploaded artifacts are controller records and are mirrored. An unreported physical action remains uncertain until its provider reconciles; controller failover never recreates that physical effect on another host.

External ownership/session operations are rejected by the managed controller boundary because their separate coordination databases are outside this domain. Migration must stop on unresolved source session/ownership protocols. Native public pairing supports begin and finish only; approval still requires a verified operator call.

## Qualification

The unit suites cover exact signed aliases, operator/receipt continuity, frozen source and passive-home fences, future record kinds, changed-bucket snapshots, pending effect preservation, native authorized assistant control, callback/cursor restart, old source feed continuity and stream signing aliases. The daemon boundary suite verifies that missing publication or stale grants suppress replies.

Production qualification additionally requires both real helpers and the real coordinator: publish a mutation, remove the owner, verify the survivor exposes the same original responsibility/run/checkpoint and no second operation, then return the former owner as a passive caught-up replica. Evidence must distinguish local commit, peer durable acknowledgement, witnessed publication and actual provider completion.
