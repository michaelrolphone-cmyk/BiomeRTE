# Future-only maintenance output retention

## Source provenance and scope

The original audited Mac agent was
`~/.local/share/biomerte-maintenance/agent.py`, SHA-256
`7fe3a8af039923586e3e0ca81b5d693a32e1a18bc44b55740766e0898b627db9`.
Its command stdout/stderr files and complete SQLite result payloads had no
retention limit, including after successful completion delivery.

The subsequently verified live agent removed the signed-phone execution gate;
its exact SHA-256 is
`04c5053a6e47994f6b73df041faccb2eaa9980ea893b73d207d05100268f16dc`.
The staged `deploy/maintenance-agent/agent.py` preserves that approved change.
The device bearer/TLS exchange, command arguments, command IDs and independent
maintenance transport remain intact. There is no new enrollment or transport.
`output_retention.py` is an independent standard-library helper, not a controller
runtime dependency. Source provenance is recorded in
`deploy/maintenance-agent/provenance.json`.

This is source for a future coordinated rollout, not a deployed change. No
maintenance service has been restarted by this work. Do not replace/reload the
live agent while VNC or any other maintenance command is active. The helper must
be present before the new agent starts. The existing embedded installer is not
regenerated here; publication/integration must select these two staged files,
not overwrite them with an older embedded agent.

## Exact completion receipts

The old gateway returned `{accepted:true}` even if no claimed command matched.
That is not proof that a particular result was stored. The gateway patch reads
back the stored command under the authenticated host and returns:

```json
{"accepted":true,"id":"command-id","host":"mac-maintenance","result_sha256":"canonical-result-digest"}
```

It acknowledges only an exact durable result. An identical completion retry,
including recovery after a lost HTTP response, returns the same receipt.
Unknown IDs, a foreign host, unclaimed work, and conflicting results receive no
positive acknowledgement. Existing clients still receive `accepted:true` on
successful completions. New retention requires all receipt fields to match.
Legacy generic receipts do not unlock cleanup of new commands.

Publish the compatible gateway receipt implementation before activating native
retention. A generic/incorrect/delayed ACK leaves new output pinned and retries
delivery; it never causes re-execution. Gateway version 39 source
`90bfda69bdca0c6e81bc075a34d0cb077d340e93` is the live gate-removed integration base.

## Future eligibility and preserved operational records

Only a newly inserted command is atomically entered into the separate retention
eligibility table. No pre-existing command is enrolled, even when replayed,
reported, old or large. Legacy payloads and files are not purged.

A future command becomes eligible only after all of these are established:

- The command returned, its original process group is gone, and its result is
  succeeded/failed rather than uncertain.
- Both published stdout/stderr text fields match the captured files. Raw-byte
  sizes, hashes and file identities are retained for later verification.
- The matching command-ID, host and full-result digest ACK has been saved locally.
- Before removing file copies, their identities/hashes still match that exact
  publication and native inspection finds no other process holding them open.

Running, nonquiescent, uncertain, unacknowledged, partially published, modified,
unsafe or still-open output is pinned. An agent crash converts in-flight work to
uncertain while retaining its eligibility fence and output. It never retries
execution. Loss of an ACK or a crash during cleanup can retry delivery/cleanup
idempotently without running a command again.

Command-ID rows and request/result identity digests do not expire. Retired result
bodies become small explicit tombstones retaining state, exit code when present,
and the original result digest. The command's PID file and directory are not
recursively removed. Only its known stdout/stderr diagnostic copies can be
unlinked. User files, arbitrary command workspace files, other journals and old
history are excluded.

## Configuration and bounds

Optional `output_retention` in the existing maintenance config accepts these
nonnegative integer settings:

- `output_count`: 100 completed acknowledged output pairs.
- `output_bytes`: 67,108,864 total raw output bytes.
- `result_count`: 1,000 full acknowledged SQLite result payloads.
- `result_bytes`: 4,194,304 total full-result payload bytes.
- `max_age_seconds`: 604,800 (7 days since completion, not since delayed ACK).

Within each category, the newest completed candidates are kept only while count,
byte and age bounds all permit. An individually oversized diagnostic is retired
after exact ACK rather than exempting it from the byte budget. Cleanup is checked
at most once a minute. Steady-state retained copies are inspected by metadata;
native lsof checks are used only when a copy is actually due for removal.

These are bounds on future eligible diagnostic payloads, not all disk use:
legacy history, active/unacknowledged/uncertain work, replay fences, and modified
or open files remain protected. SQLite may reuse freed pages instead of shrinking
its file immediately; this code does not VACUUM historical databases. In-flight
command output and the existing full-result publication memory path are unchanged.
The paired gateway patch also bounds newly admitted terminal result payloads by
count, bytes and age, preserving old history and exact result-digest replay fences.
A lost/delayed ACK can be recovered from that fence after gateway payload retirement;
local copies stay pinned until that exact receipt is saved. Gateway result reads
explicitly return result_retired=true after payload expiry. This is a recent
diagnostic window, not an indefinite download guarantee.

Native cleanup depends on complete lsof visibility. It fails closed on inspection
warnings, unexpected output or unknown state. As with other external filesystem
cleanup, a new unrelated process can race a last inspection; no claim is made
that arbitrary noncooperating user writes are atomically locked.

## Validation and rollout guard

Tests cover exact/mismatched/generic/delayed ACKs, lost-ACK replay, duplicate and
conflicting IDs, atomic command admission, ACK/cleanup crash points, immutable
legacy history, protected running/uncertain work, native writer-inspection
failures, raw-output changes, count/byte/age limits, and a real local child process
through the staged agent function. They do not constitute Mac deployment.

Before deployment, reconcile the current live agent hash, ensure all commands
are settled and VNC maintenance is inactive, integrate the gate-removed source
and gateway exact-ACK patch, and qualify native lsof behavior. No historical purge
or speculative replay is part of this rollout.
