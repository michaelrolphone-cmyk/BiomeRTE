# Bounded-memory checkpoint retention, format 2

This supersedes the earlier whole-state format-1 prototype. No live changes were performed by the implementation worker. Retention remains disabled unless explicitly configured.

## Memory and recovery model

The gateway stores a ≤32KiB manifest with 256 first-level hash routes. Each route points into an adaptive radix trie of descriptor nodes capped at512KiB. Leaves describe record keys and content-addressed value chunks; branches split as descriptors grow. There is no fixed total-record ceiling from the initial256 routes. Value chunks contain at most65,536 UTF-16 code units (≤256KiB UTF-8); surrogate pairs are not split. Raw value JSON remains exact, including native large integers and float serialization.

A builder reads one bounded journal entry and handles four changes per invocation by default (maximum16). Its sequence, entry offset, and shard roots are durable SQL state. A completed entry can resume publication after interruption without requiring another journal append. New immutable objects are read-back/hash verified before publication. Current/prior root replacement and root reference edges are atomic through SQL triggers. Unchanged COW nodes inherit prior content verification. A complete snapshot is never assembled in a Worker Map or string.

The content DAG has SQL reference counts and an upload grace period. GC claims unreachable objects atomically before deletion; guards prevent a concurrent reader from pinning an object already claimed for removal. Failed GC remains retryable. A renewable per-controller restore lease pins both the snapshot DAG and its journal-tail floor, so advancing checkpoints cannot strand a bootstrap. At most two unexpired restore leases per controller are admitted. Expired readers are removed conditionally, without racing a successful renewal.

Authenticated checkpoint routes expose manifests and up to eight bounded objects per page, plus lease renewal/release. Controllers cannot supply checkpoint state or change the retention boundary. `journalRecord` looks up one trie path and prefers newer checkpoint state over older retained journal entries, including tombstones, so revoked peers cannot reappear from protected legacy history.

Native downloads stage verified records into temporary SQLite beside the mirror database, outside the live writer transaction. Trie walking, duplicate-key checking, and value decoding are bounded by a node/value. The fetched journal suffix is also disk-backed. Restore streams records into the live mirror, rebuilds existing projections through normal writes, checks the tail against the observed head, and atomically commits position. Canonical replay clears `Records.changes` per record/entry to prevent accidentally accumulating the full state in memory. Reference-index triggers receive ordinary canonical DELETE/INSERT operations. Restore pins release only after the entire suffix is locally staged, before taking the mirror writer lock. Failed validation rolls back installation.

## Input admission and historical preflight

A single journal entry or canonical record must be ≤4MiB UTF-8 by default. `JOURNAL_MAX_ENTRY_BYTES` may lower this limit (minimum4096 bytes), but cannot raise it above the bounded parser's4MiB capacity. The same limit governs new-write admission and checkpoint ingestion.

The authenticated append route bounds the outer request body before decoding and rejects an oversized entry with a clear HTTP413 before archival or journal commit. The shared `storedJournal` writer also enforces admission for recovery writes. Native transport preserves413 so a rejected proposal rolls back atomically and existing tick-budget reduction can split work. Explicit archival of preexisting rows is exempt from new-write admission.

Existing oversized archive bodies fail closed from R2 size metadata before decoding. Progress exposes `max_entry_bytes`, `max_entry_seq`, `blocked_seq`, `blocked_bytes`, and `last_error`; admission exposes durable `rejected_entries`, `last_rejected_bytes`, and `last_rejected_at`. A blocked historical entry is identified without deletion. A harmless read of an earlier entry does not erase that blocker.

Before enabling cleanup, build through the full historical journal with cleanup disabled and verify these counters and final sequence/hash. Largest canonical-record size alone is insufficient. Arbitrary-size transactions would require a separate resumable range-based parser/hash-state protocol; they are not silently admitted and allowed to stall retention indefinitely.

## Rollout

1. Apply the gateway v2 delta over the original checkpoint patch. Include `scripts/build.sh`: it must bundle `lib/journal-checkpoint.mjs`. Preserve deployed46813aa transport acknowledgment changes.
2. Apply native v2 delta over the original checkpoint patch. It changes catch-up/staging, not the later acknowledged-append optimization. Preserve installed transport provenance and integration changes.
3. Keep cleanup disabled. Upgrade every replica. Verify largest journal input, checkpoint catch-up, empty-directory restore, existing-replica convergence, and current/prior recovery on the actual deployment. These live checks are still outstanding.
4. Capture existing headN. Approved future-only retention uses `JOURNAL_RETENTION_ENABLED=1` and `JOURNAL_RETENTION_FROM_SEQUENCE=N+1`. Missing/invalid settings cannot delete history. Lowering the boundary to1 permits removal of preexisting history and requires separate explicit approval.
5. Retain the previous checkpoint anchor and the complete suffix, additionally respecting active restore floors. An older reader without this protocol cannot rebuild from trimmed history; do not downgrade it after cleanup without restoring full history.

Existing format-1 checkpoints are not auto-converted. If a prototype was published, migration must be handled explicitly with cleanup disabled. Existing pre-checkpoint append-CAS archive orphans are not bucket-scanned or purged. Canonical `_receipt` and `_receipt_stream` records remain authoritative, and no identity-expiry policy is inferred. Native SQLite free pages remain reusable; physical vacuum is separate maintenance.

## Local verification

- Gateway benchmark builds and reads600 records totaling210,000,000 payload bytes under Node `--max-old-space-size=64`; checkpoint objects stay below512KiB and the root is~16KiB. The same constrained-heap run also publishes an11-record,3,850,659-byte transaction near the configured input capacity. Test storage is disk-backed, not a full-state in-memory fixture.
- Native benchmark restores the same210MB logical state with disk-backed source/staging and measured peak Linux RSS23,532KiB after removing the replay accumulator.
- Tests cover raw numeric preservation, tombstones/peer authority, partial-entry resume, failed read-back, interrupted publication, fallback root, explicit input-bound rejection, atomic413 rollback, durable rejection/progress metrics, rolling DAG GC, adaptive shard splitting, and long-reader pins through root rotation and GC.
- Full suite and final benchmark logs are in the integration workspace: `/workspace/shared/checkpoint-gateway-v2-tests.log`, `/workspace/shared/checkpoint-native-v2-tests.log`, `/workspace/shared/checkpoint-shards-memory.log`, `/workspace/shared/checkpoint-native-memory.log`.

The benchmark demonstrates bounded application memory, not a measured production Cloudflare RSS/CPU guarantee. Production resource limits and throughput still require deployment validation before cleanup enablement.
