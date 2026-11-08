# Worker persistence coverage and future reconciliation retention

The shared `biomerte.worker` implementation runs on both Pi and Mac. This change
closes a confirmed redundant-copy gap: an acknowledged action previously lost
its full `actions.result`, but its `reconciliations.previous_result` and
`reconciliations.result` remained full indefinitely.

## Reconciliation lifecycle

Only a reconciliation row first inserted by this version, for an action with an
exact matching `result_retention_eligible` signature, receives a
`reconciliation_retention_eligible` row. No startup scan or historical enrollment
occurs. Existing reconciliation rows remain byte-for-byte intact, including
those whose actions already have the older action-retention marker. Unmarked
legacy actions cannot enroll a new reconciliation copy.

The new marker preserves the operation ID, action identity signature, source
(`controller_result` or `controller_recovery`), original authority result digest,
and eventual compaction time. The existing audit row keeps its operation ID,
terminal state and observation time. Once the local authority acknowledgement is
durable, the two redundant payload columns become small JSON summaries with
`payload_expired: true`, exact UTF-8 byte count, and SHA-256 of the exact stored
text. For a controller recovery summary, this byte digest is deliberately
separate from the digest of the original full authority result.

A separate `reconciliation_debug` rolling window retains the most recent full
pairs, at most 100 rows and 1 MiB of combined encoded payload. Individually
oversized pairs are not copied. Its limit is independent of the existing
`result_debug` limit of 100 rows and 1 MiB. Thus the two disposable windows retain
at most 2 MiB of encoded payload total, plus bounded row metadata. These are not
limits on SQLite file size, legacy rows, unresolved work, IDs, or required
identity metadata.

Normal reconciliation adoption leaves the full local terminal outcome pending
until verified acknowledgement recovery or exact result re-delivery. An expired
authority recovery summary is already bound and acknowledged, so its new audit
copy compacts in the adoption transaction while its small recovery summary
remains in `actions`. Audit enrollment/adoption, acknowledgement, debug insertion,
compaction and recovery-index changes use the existing SQLite transactions. A
failure or process crash restores the prior state; no effect is retried.

Explicit `resolve-effect` retries of already acknowledged outcomes verify the
exact authority outcome and return without rewriting local payloads. Newly
resolved full outcomes, including those confirmed by an expired authority,
remain in the pending-result projection until the normal worker can atomically
retire them with its configured adapter cleanup intent. This avoids both
rehydrating discarded payloads behind an ACK and bypassing adapter cleanup.
Legacy full outcomes retain the prior preservation behavior.

## Complete shared-worker persistence inventory

- `fences`: resource generation and holder, required for physical exclusion.
  No result payloads or historical log append.
- `actions`: operation/signature/state/resource are permanent no-replay fences.
  Eligible acknowledged full outcomes move to bounded debug; unresolved,
  unacknowledged and legacy payloads remain available for exact recovery.
- `result_retention_eligible`: signature-bound future-enrollment evidence.
  Metadata only; never backfilled from legacy rows.
- `result_acknowledged`: durable acknowledgement IDs. Prevents resubmitting a
  cleared outcome or re-executing the effect. Metadata only.
- `result_debug`: existing 100-row/1-MiB rolling encoded-result window.
- `result_cleanup`: pending idempotent adapter hook argv and operation/result
  digest only, not the full outcome. Removed after successful cleanup. Failures
  remain pending and must not be discarded merely due to age.
- `reconciliations`: original state/observation provenance plus future compact
  payload summaries as described above. Legacy rows are preserved.
- `reconciliation_retention_eligible`: new signature/source/result-digest
  provenance and compaction marker, retained as metadata.
- `reconciliation_debug`: new 100-pair/1-MiB combined-payload rolling window.
- `result_recovery_pending`: indexed outstanding terminal receipts, removed on
  ACK. Polling consults this projection, not completed action/audit history.
- `result_recovery_schema`: the small projection schema-version record.
- `authority`: one controller/site/realm/provider namespace binding.
- SQLite's `sqlite_sequence`: debug-table sequence counters only. SQLite journal
  or WAL sidecars remain durability machinery, not disposable app history. This
  change does not VACUUM, shrink the database, change journal mode or purge old
  bytes; SQLite may reuse freed pages for future writes.
- `worker-health.json`: the current health snapshot, replaced rather than
  appended. Future writes use exactly one reusable `.pending-v1` staging file
  and one empty `.write-v1.lock` file. A separate stable flock serializes
  concurrent writers. Opens are no-follow/nonblocking, regular single-link
  files and inode identity are checked before writes, and publication uses
  fsync plus atomic replacement. Crashes leave at most that one future staging
  snapshot; the next write reuses it. Existing UUID staging files are neither
  scanned nor deleted.
- `worker.lock`: the existing empty singleton-worker lock, not an append log.
- Adapter stdout/stderr pipes: in-memory execution results, persisted through
  `actions` only. Cleanup hook stdout/stderr go to `DEVNULL`. Worker warnings
  use the service's configured log destinations; service-log rotation is a
  separate retention layer, not another worker-managed payload table.

The private-chat subclass independently keeps only signatures/state and `{}` on
disk, with result retry payloads in memory. Pi terminal upload caches/output
files and Mac `terminal-results` receipts/stdout/stderr/published results are
adapter-owned persistence, handled by their existing eligibility and exact
acknowledgement hooks. This patch does not change their retention policy or
remove any of their historical files. Generic adapters that do not configure a
cleanup hook retain their own outputs under their own contracts; the shared
worker cannot infer that those files are disposable. Controller journals,
backups, user artifacts and external service logs are likewise separate layers.

## Restart checks and qualification

The original five-column `reconciliations` schema is unchanged. An operational
restart checker must take the durable `result_acknowledged` branch before
requiring a full audit result/quiescence object. Unacknowledged adopted outcomes
still retain their full audit and action payloads. Compacted JSON summaries must
never be submitted as original provider output.

Tests cover large previous/new payloads, exact byte digests, combined byte/count
bounds, legacy rows, signature mismatch, delayed/lost acknowledgements, expired
authority summaries, repeated reconciliation and replay, transaction failures,
actual process death during compaction, pending-index behavior, operator
recovery with cleanup, crashed health publication, unsafe links/nonregular
paths, and concurrent health writers. The shared module uses Python 3.4 syntax
and SQLite 3.8 SQL without JSON1, UPSERT, or new SQL features. Grammar validation
is separate from testing on an actual old interpreter/database build.

### Current health and historical uncertainty

`blocked` reports failures of the exact currently outstanding action/fence identity
(kind, ID, generation and guard), or a corresponding still-running future.
A canceled or replaced candidate cannot leave a permanent degraded-health latch.
A failed poll supplies no authoritative empty inventory and therefore cannot clear
reporting state. Removing a stale reporting entry never changes the actions,
fences, reconciliation audit or execution outcome, and never dispatches/replays work.

`historical_debt` separately lists up to 64 uncertain operations on guards with no
current work, with a truncation indicator. Its partial index excludes completed
history; the bounded read does not walk the completed-action ledger. Such debt
still blocks a future fence until standard authenticated reconciliation proves
quiescence. Ready health means current work is not blocked, not that all historical
effects are known or that old operations may be repeated.
