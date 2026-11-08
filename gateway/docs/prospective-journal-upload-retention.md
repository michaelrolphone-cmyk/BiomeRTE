# Prospective managed journal uploads

The new upload lifecycle is opt-in with `JOURNAL_MANAGED_UPLOADS=1`. It uses the dedicated `controller-journal-managed-v1/<site>/<fresh-namespace-UUID>/` namespace only. Existing `controller-journal/` objects, including unidentified historical orphans, are never enumerated by its collector. A fresh namespace UUID is persisted before the first managed upload; the collector never scans other namespace UUIDs, including unidentified objects predating activation. No new credentials, controller role, or enrollment authority are introduced.

## Lifecycle and race safety

Each attempt gets a fresh random UUID and object key. A durable pending intent precedes PUT; token reuse and renewal are not supported. The pending lease is ten minutes. An INSERT trigger requires matching live pending site/token/hash/sequence, and an AFTER INSERT trigger commits that intent in the same D1 transaction as the journal row. The collector atomically changes only expired pending intents to collecting. Thus either journal commit wins and the blob is protected, or collection wins and later journal commit is refused. A lost response after commit does not make a committed object collectable. A retry writes a different key, so delayed deletion of a losing attempt cannot remove its replacement.

Expired intent collection deletes the exact attempt object before removing its metadata. Unknown delete outcomes retain collecting metadata for retry. A PUT that completes later than this cleanup can recreate its own old key, but cannot commit a journal row. Bounded cyclic listing of the dedicated managed namespace finds and removes that late object on a later cycle. No maximum PUT duration, multipart-abort guarantee, whole-prefix scan in one request, or permanent per-object tombstone is assumed.

This protocol assumes managed keys are written only by this lifecycle, tokens are fresh UUIDs and never reused, and D1's atomic trigger semantics remain intact. It does not attempt to repair arbitrary manual corruption of the intent tables. Unknown/malformed keys are preserved rather than guessed to be disposable.

## Bounds and progress

- At most 256 pending/collecting intents per site. Admission uses an indexed count and fails before PUT when full; committed intents do not consume that admission allowance.
- Expired intents are selected through `(site,state,expires)`, never a full object or journal scan.
- Collector API accepts at most 32 expired intents and 32 listed objects per invocation; checkpoint bootstrap calls it with 8. The persistent cursor is one row per site and cycles back at the end. Late objects behind a cursor are found on a later cycle.
- Active and committed intent rows are protected. Committed metadata is retired by a journal DELETE trigger when the authorized journal retention path removes the journal row.
- Any managed cleanup yields a GC-only checkpoint response before creating more checkpoint objects in that invocation.
- Separately, a saturated 128-object derived-checkpoint GC batch yields a GC-only round rather than creating more checkpoint objects. Repeated rounds drain debt before building resumes. Bounded cumulative `gc_removed_total` and `gc_only_rounds` counters are returned in checkpoint status.

Discovery is eventual: the interval until a late object is found depends on live managed namespace size and available scan throughput. An arbitrary producer faster than the collector cannot have a guaranteed fixed storage deadline. The 256 unresolved-intent limit provides admission backpressure, while normal committed storage is bounded by the separately authorized rolling journal policy. No numeric live cleanup-latency guarantee is claimed.

## Compatibility and rollout

Readers, checkpoint ingestion and journal compaction accept both original and managed pointers. Native transport APIs and raw journal hashes are unchanged. Only future writes enter the managed namespace; historical archival remains in its original namespace. Existing checkpoint bootstrap does not require enabling authoritative journal deletion for derived GC or managed orphan cleanup.

After managed uploads are activated, keep the dual-format gateway readers on rollback. Turning the flag off stops new managed uploads and managed collection, but does not convert existing managed pointers to the old format. Do not roll back to a gateway that cannot read these pointers. Parent deployment coordination owns activation; this patch performs no live mutations by itself.

## Local qualification

Ten focused tests cover committed lost-ACK protection, duplicate CAS losers, delayed PUT after intent retirement, late commit fencing, GC/commit races, unknown DELETE outcomes, bounded cyclic listing including keys behind a cursor, active intent protection, 256-intent backpressure, mixed-format replay/checkpoint/compaction, delayed DELETE versus a newer retry, crash after PUT, explicit legacy-format inline migration under the managed flag, and derived GC-only debt draining.

Full gateway suite: 108 tests passing. Native checkpoint restore compatibility: 8 tests passing. ESM build and artifact validation pass. A 7,700-entry backlog test with 16 new appends per build drains in 197 invocations, reaches head 11,172, completes 20 steady-state rounds, preserves legacy receipts, and observes at most 573 mocked service calls per invocation. These are local qualification results, not production latency measurements.
