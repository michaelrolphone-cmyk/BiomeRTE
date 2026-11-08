# Future-only maintenance operational-payload retention

This extends the existing authenticated maintenance device endpoint. It preserves
immediate owner-authenticated admission and adds no transport, credential,
enrollment, execution gate, or authorization mechanism.

## Record classes and why they are retained

- `maintenance_hosts`: enrolled credential verifier, phone identity, enable/revoke
  state and last-seen/last-maintenance timestamps. These are live authority and
  routing data, not disposable diagnostic history. No host is deleted or reenrolled.
- `maintenance_jobs`: active, awaiting-confirmation, needs-review, cancelled,
  rejected and uncertain requests remain whole. Uncommitted results remain whole.
  These records can still determine execution, recovery or authority settlement.
- Future succeeded/failed results: their full diagnostic bodies use the existing
  result count/byte/age window, then become small explicit result tombstones.
- Future succeeded/failed requests with no signed confirmation dependency: their
  executable/argv/cwd and repeated approval question use a new request window,
  then become small request tombstones. Exact request and approval digests remain.
- `maintenance.submit` audit bodies: the redundant argument copy shares its job's
  request window and is compacted atomically with that request. The original audit
  body digest, action, host, ID, creation time and explicit tombstone remain.
- Enrollment/revocation audits, signed confirmation receipts and cancellation
  reservations/receipts: unchanged. They carry control-plane provenance, exact
  authenticated reply replay or unresolved authority settlement. This patch does
  not classify them as disposable or change their recovery contracts.

The terminal ID/host/state, exact request/result/approval digests, timestamps and
small audit tombstones are permanent replay fences. Their row count can grow.
Bounding full diagnostic payloads does not mean deleting the evidence that keeps
an old command ID from executing again.

## Prospective eligibility, no history backfill

A new command's INSERT atomically sets `retention_version=1`. The request patch
also sets `request_digest`, `request_bytes` and `approval_digest` at that insert.
Its submit audit is inserted in the same D1 transaction with an explicit marker,
body digest and byte count. Existing jobs and audit rows are never marked by
migration or duplicate admission. In particular, jobs admitted by the earlier
result-only version retain NULL request digests and remain excluded from request
cleanup, even when their existing result-retention marker is 1.

Request cleanup requires the prospective marker and all request identity fields,
a succeeded/failed terminal state, a durable exact result digest, a completion
time, and no `confirmation` dependency. Request and audit retirement commit in
one D1 batch. Its update predicates recheck identity and authority conditions
rather than trusting a possibly stale candidate scan. Existing unmarked audit
rows, audits for other operations and other hosts are never changed.

No migration deletes payloads, hashes historical bodies or scans history to enroll
it. Creating the new sparse index is ordinary schema work; recurring maintenance
uses only the bounded indexed candidate queries described below.

## Exact replay and ACK recovery

Completion atomically records the canonical result digest, payload byte count,
completion time, terminal state and full result. The gateway reads back identity
and content before returning `accepted=true`, command ID, host and result SHA-256.
Unknown, unclaimed, foreign-host and conflicting completions are not acknowledged.

A result tombstone contains state, `result_retired:true` and the original SHA-256.
A matching delayed/lost-ACK retry returns the same exact receipt without restoring
stdout/stderr. An altered result receives 409. `maintenance.result` returns the
explicit tombstone rather than claiming that the full output remains available.

A request tombstone contains `request_retired:true` and its original SHA-256.
Submit retries compare the host and digest of the exact original request encoding,
including its command ID and proposal. This preserves prior equivalence rules:
omitted/null cwd and irrelevant extra argument fields do not change the command;
changed argv, argv order, cwd or host do. Identical retries return the current
terminal state and never queue or execute the command again. They do not restore
request or audit payloads. Legacy requests continue using the full original
request comparison without being enrolled.

The retained approval digest and deterministic proposal identity also allow a
late signed phone response to receive its existing `already_settled` outcome,
without storing the large approval question or granting new execution authority.
Signed envelopes and response-ID fences retain their previous settlement rules.

Retirement can occur after the durable completion commit even if the first HTTP
ACK is lost. There is no new assumption that network delivery is proof of receipt:
the native agent keeps local output until it receives and persists the exact
command/result ACK. The gateway's digest fence can regenerate that receipt after
payload retirement. Missing durable completion, active work and uncertain outcomes
cannot qualify for request retirement.

## Count, byte and age windows

Per-host defaults, configurable through the existing environment:

- `MAINTENANCE_RESULT_MAX_COUNT`: 1000 full future terminal result payloads.
- `MAINTENANCE_RESULT_MAX_BYTES`: 4194304 aggregate full result-payload bytes.
- `MAINTENANCE_RESULT_MAX_AGE_SECONDS`: 604800 seconds since completion.
- `MAINTENANCE_REQUEST_MAX_COUNT`: 1000 future terminal request/audit pairs.
- `MAINTENANCE_REQUEST_MAX_BYTES`: 4194304 aggregate bytes across full request
  bodies, including the repeated approval question, plus their full submit audits.
- `MAINTENANCE_REQUEST_MAX_AGE_SECONDS`: 604800 seconds since completion.

The result and request windows are independent. Keeping a result for inspection
does not require retaining its original command body indefinitely, or vice versa.
Limits must be nonnegative safe integers; malformed/empty/boolean settings fail
closed. Each count is capped at 10000 to bound scan work.

Each class has a partial index containing only eligible unretired future terminal
rows. Each pass reads at most its configured count + 200 metadata rows, at most
10200 per class, and selects at most 200 retirements per class. Request candidates
perform at most one indexed primary-key audit lookup each; no payload bodies are
read for budgeting. The request update and audit update are one atomic pair per
selected command. A full batch leaves maintenance immediately due so polling can
drain another bounded batch. Otherwise polling checks at most once per minute;
completion forces a bounded pass.

The newest candidates are retained subject to count, byte and age limits. An
oversized pair/result does not consume the byte allowance for smaller recent
payloads. Bounds apply after maintenance drains any backlog, not as an
instantaneous hard physical database-file cap. Legacy history, active/uncertain
work, authority records and permanent small replay fences are outside these
payload windows. SQLite/D1 can reuse freed pages rather than immediately shrink
its allocated storage.

## Interruption and deployment compatibility

A failed new-admission batch leaves neither a queued job nor its audit copy.
A failed request-retirement batch leaves both full copies intact, including all
identity fences. Cleanup failure never invalidates an exact durable completion
receipt; the response signals pending maintenance and later polling/completion can
retry. A lost response after successful compaction is recovered by exact replay.

Once request tombstones exist, rollback code must understand their request and
approval digest fences. Rolling back to a version that compares only full request
text would reject otherwise-identical submit retries. Additive database columns
alone do not make such an old binary a safe rollback target.

Tests cover exact/changed/concurrent submit and completion retries, Unicode and
extra audit bytes, prospective and historical schema cutovers, active/uncertain
and authority-dependent protection, per-host isolation, count/byte/age bounds,
indexed bounded scans, 200-pair backlog draining, atomic admission/cleanup failure,
late signed phone receipts, and changed authority during candidate selection.
