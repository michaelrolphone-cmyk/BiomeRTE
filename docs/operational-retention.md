# Future operational payload retention

Application commands and ticks enroll only newly created records. Existing
records, including legacy receipts, are never backfilled or deleted by this
policy. Merely updating an existing record does not enroll it. Database file
shrinkage/VACUUM and historical deletion are separate operations.

The default node configuration is:

```json
{"operational_retention":{"enabled":true,"seconds":86400,"keep":1024,"batch":128,"interval":60}}
```

For each kind, eligible payloads expire after one day or when older than the most
recent 1,024 completed records. A sweep changes at most 128 records, at most once
per minute of application ticking. Idle ticks with nothing eligible add no
retention transactions after the initial activation marker. The oldest eligible
records expire first within each dependency-priority group. Clocks should be
synchronized; elapsed retention is based on wall time, not tick count.

## What survives

Active, uncertain, and referenced records survive. User knowledge, unclassified
record kinds, pending recovery state, live workflow subscriptions, pending
assistant wake events, and receipts that still promise a result are preservation
roots. Reference reachability is transitive across JSON values and keys. Released
lease task bindings and compacted identity summaries no longer promise payloads.
Quiescent terminal outputs can expire. Their summaries preserve the exact
worker identity digest, original result digest, and minimal quiescent evidence
for disconnected workers, without retaining stdout or artifact references.
No user artifact is collected merely because of its age: an artifact must be
newly enrolled, explicitly uploaded with `artifact.put` `transient: true`, and no longer
referenced by retained data. The default upload is durable. A durable upload of the same bytes permanently
protects a transient chunk. Historical artifacts remain intact. Bundled terminal
output uploaders must opt into the transient flag during rollout; private image
outputs and other user artifacts should remain durable.

New terminal run/action payloads compact to an identity/state summary with
`result_expired: true`. New inactive combined submissions compact similarly.
Version-2 receipts compact to their signature, format, operation, creation time,
and explicit expiry marker. Version-1 receipts and `_receipt_stream` records
are never compacted. Compacted identities are never executed again. Status,
ordinary result replay, and learned-routine requests fail explicitly with HTTP 410 when
the requested payload has expired. `cap.status` with explicit `recovery: true`
returns the bound expired recovery summary to authorized callers. A late
`provider.result` with exactly the original state, provider, generation, and
result digest is acknowledged without executing effects or restoring payloads;
a conflicting outcome is rejected. Update recovery-aware workers together with
the controller before enabling this expiry behavior. Missing artifacts after activation report
"unavailable or expired", without falsely asserting that every missing ID was
previously stored.

Event removal advances an explicit history floor. Cursors below that floor fail
with HTTP 410 and require snapshot resynchronization; retained legacy events can
still be fetched individually. Active internal process subscriptions/wake input
pin events they have not consumed. External subscribers do not pin history
forever. The history floor is conservative and can cover retained events before
a gap. No event cursor is silently advanced.

## Boundaries and cost

This bounds *eligible debug payloads*, not the entire database. Small per-operation
identity fences intentionally remain to prevent replay under the existing
unbounded caller-chosen-ID contract. Leases, authorization/reconciliation records,
user knowledge, active work, preserved legacy history, pinned data, and their
metadata can still grow. Retention cannot safely replace that contract with a
hard database-size cap. Bookkeeping for retained active/pinned/new user artifacts
also remains. A future ID-lifetime protocol would be required to bound identity
counts without risking repeated external effects.

Each sweep streams preservation roots rather than loading historical records
into another full in-memory database. It still reads those roots (up to twice)
and loads enrolled operational records. On large sites the retention interval
should be tuned after measuring runtime latency; a reference index is a future
optimization, not an excuse to discard unclassified references.

References must be explicit JSON IDs/keys, as used by runtime records. Opaque
encoded artifact content is not traversed as an executable reference manifest.
Do not rely on debug artifacts for the sole durable copy of user knowledge.

For representative short IDs, compacted action identities occupy approximately
683 JSON bytes including quiescent recovery digests/evidence, receipts 170 bytes, and run summaries
227 bytes in the synthetic measurement (32-character IDs, 64-character provider
and revision IDs). These replace 10,418-, 10,154-, and 10,217-byte example
payloads respectively. One million such retained identities alone would still
occupy about 683 MB, 170 MB, and 227 MB of canonical JSON respectively. Actual storage also includes SQLite indexes, IDs, journals,
and provider-side records. These are not database byte limits.
