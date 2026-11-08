# Future-only maintenance result retention

This change extends the existing authenticated maintenance device endpoint. It
preserves the version-39 immediate-admission gate removal; it adds no transport,
credential, enrollment or authorization mechanism.

## Eligibility and replay

A new command's INSERT atomically sets `retention_version=1`. Existing commands
remain NULL, including on a repeated submit, and are never enrolled or purged.
Queued, claimed, awaiting-confirmation, cancelled, rejected and uncertain work is
excluded from result cleanup. Only future succeeded/failed payloads participate.

On completion, the gateway records the full canonical result digest, payload
byte count and completion time atomically with the terminal state and payload.
It reads back ownership/content before replying with accepted=true, command ID,
host and result_sha256. Unknown, unclaimed, foreign-host or conflicting
completions are never acknowledged.

A retired payload becomes an explicit small result object containing state,
`result_retired:true` and its original SHA-256. The command ID, request, terminal
state and immutable result digest remain replay fences. A matching duplicate or
late/lost-ACK retry returns the exact same receipt without restoring the payload.
An altered duplicate receives 409. Retirement never queues or re-executes work.

`maintenance.result` returns that tombstone after expiry. It does not claim that
the original stdout/stderr is still available. Before expiry it returns the full
result as before. Old command payloads retain their original behavior forever
unless a separate historical-purge action is authorized.

## Budget and bounded work

Per-host defaults, configurable through the existing environment:

- MAINTENANCE_RESULT_MAX_COUNT: 1000 full future terminal payloads.
- MAINTENANCE_RESULT_MAX_BYTES: 4194304 aggregate full-payload bytes.
- MAINTENANCE_RESULT_MAX_AGE_SECONDS: 604800 seconds since completion.

Limits are nonnegative safe integers. Count is capped at 10000 to bound each
metadata scan. Cleanup uses a partial index containing only unretired future
succeeded/failed results and examines at most configured count + 200 rows, never
more than 10200. It reads metadata, not result bodies, for budgeting, and updates
at most 200 payloads per invocation. A full batch leaves maintenance immediately
due so the next poll can drain another bounded batch. Otherwise age cleanup runs
at most once per minute. Completion also triggers a bounded pass.

The newest candidates are retained subject to count, byte and age limits. An
oversized payload is retired, without consuming the byte budget reserved for
smaller recent results. Old history, active/uncertain work, request/replay fences
and small tombstones are outside the diagnostic-payload budget. Bounds are
maintenance-pass bounds, not an instantaneous hard physical database-file cap.
SQLite/D1 may reuse freed pages rather than immediately reducing allocated size.

The retained per-command bookkeeping is fixed-size version/time/byte fields and
a 64-character result digest, alongside the existing command identity. No new
append-only polling or maintenance-history table is introduced. Original command
requests and existing audit history are unchanged by this result-specific patch.

## ACK and local-copy behavior

The gateway can retire a future payload after committing its exact result,
including while the initial HTTP receipt is lost. The native retention agent
keeps local output until it receives and saves the exact command/result ACK; a
retry recovers that receipt from the digest fence even after gateway retirement.
Thus a delayed ACK cannot trigger local cleanup early, and duplicate delivery
cannot regrow retired gateway payloads. This is diagnostic-window retention, not
an indefinite result-download guarantee.

Cleanup failure leaves the committed payload/fence intact and does not invalidate
an exact completion receipt. Responses signal a pending maintenance failure;
subsequent polling/completion can retry. No old-history purge is performed.

Tests cover exact and delayed ACK, same/changed replay after compaction, atomic
new-command eligibility, old-history preservation, count/byte/age boundaries,
active/uncertain protection, bounded indexed scans, batch draining, and cleanup
failure after result commit.
