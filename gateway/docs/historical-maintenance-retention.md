# Historical maintenance and service-log roll-off

This migration corrects the earlier blanket exemption for predeployment data.
Historical diagnostics that meet the same operational safety checks are enrolled
in the existing rolling windows. Being old is neither a reason to preserve an
otherwise disposable payload forever nor sufficient proof that it is safe to
remove. No deployed host or gateway is modified by this source change alone.

## Windows and permanent identity fences

The unchanged native maintenance windows are 100 output pairs / 64 MiB,
1,000 result bodies / 4 MiB, and seven days from the original completion. Gateway
requests plus their submit audits, and gateway results, each use 1,000 / 4 MiB /
seven days. Enrollment does not reset these ages to migration time. Command IDs,
request/result digests, execution identity, approval identity, terminal outcomes,
required confirmation/cancellation records, credentials and user files survive.

Plans and apply operations have at most 128 candidate identities per batch and
use keyset cursors, not offsets. Gateway qualification has an additional 8 MiB
aggregate payload-read budget including audit copies. The same aggregate guard
applies to device evidence and migration apply, before loading full bodies. An
individually oversized record is reported explicitly without blocking pagination
to later IDs; it is not treated as successfully migrated. Native planning selects
only a bounded page of identities before checking their eligibility markers, so
large already-enrolled history cannot turn a page into a full-history scan.
Blocked rows have explicit reasons and remain available for reconciliation.

The historical inventory recorded 34 native maintenance rows and 102 command
files. That is background, not a deletion manifest. Fresh per-record qualification
determines which are eligible; no count or timestamp is inferred from that old
inventory.

## Gateway plan, evidence and enrollment

The existing owner-authenticated `biome_invoke` route accepts:

- `maintenance.retention.plan`: arguments `{host, after, limit}`. It returns a
  bounded `plan` and `plan_sha256`, without changing diagnostic payloads. The
  ordinary schema initializer may install additive columns/indexes.
- `maintenance.retention.apply`: arguments `{plan, expected_sha256}`. It requires
  the authenticated owner, verifies the plan hash and exact live preimages, and
  atomically enrolls each job and its matching submit audit. It does not run a
  sweep in that call. The existing maintenance polling/completion sweep will then
  apply the usual windows, so applying enrollment authorizes that later retirement.

Only succeeded/failed records without a confirmation dependency qualify. Intact
request/result/audit bodies are parsed and hashed before enrollment. Existing
metadata must agree with those bodies. `completed` is retained when present;
for older rows the existing terminal `updated` field is the original completion
write time. The source writes that field with terminal completion, not on read or
cleanup. Impossible ordering, missing data or inconsistent digests block the row.
No filesystem modification time, guessed duration or migration timestamp is used.

A small `execution_digest` preserves the native `{argv,cwd}` identity independently
of the gateway's full request/proposal digest. New admissions record it too.
An already-compacted older record without enough execution evidence is reported
as unavailable, not assigned a fabricated identity. Replay after enrollment or
payload retirement returns the existing result ACK and terminal submit response;
changed commands are still rejected. Retrying a committed migration is idempotent.

The existing authenticated device endpoint adds `retention_history` with at most
128 distinct IDs. It only returns records belonging to that bearer host, together
with original creation/completion times and exact identity digests. It performs
no command admission, execution, enrollment or retirement. Native migration uses
this endpoint rather than importing unverified timestamps from an inventory file.
There is no new credential or transport.

## Native plan/apply

`deploy/maintenance-agent/historical_retention.py` uses the maintenance home's
existing config, HTTPS origin and device credential. It never prints credentials.

```sh
python3 historical_retention.py plan --home /exact/maintenance/home \
  --limit 64 --after '' > historical-native-plan.json
python3 historical_retention.py apply --home /exact/maintenance/home \
  --plan historical-native-plan.json --expected-sha256 REVIEWED_PLAN_SHA256
```

Plan opens the local SQLite ledger read-only and retrieves authenticated gateway
evidence. The legacy ledger itself has no original request or completion-time
columns, so neither its generic `reported` status nor a file's apparent age
substitutes for that evidence.

Each candidate must have an intact local succeeded/failed result matching the
remote digest, a matching generated command directory, an unchanged safe PID
file, a gone original process group, stdout/stderr exactly matching publication,
and complete native inspection showing no external openers. Active, uncertain,
missing, changed, nonquiescent or authority-dependent records remain protected.

Apply repeats gateway and local qualification, obtains a fresh exact command-ID /
host / result-SHA completion ACK through the existing completion route, then
rechecks files and process quiescence after that network exchange. Only then does
one SQLite transaction save the original request identity, original creation and
completion times, exact ACK and output fingerprints. A generic old `accepted`
receipt cannot unlock cleanup. No command is run or replayed. A crash during local
enrollment rolls back, and a lost completion ACK can be retried idempotently.

Apply itself does not unlink files or compact result bodies. The existing native
maintenance pass applies its windows to enrolled records; the oldest eligible
payloads may therefore retire at the next pass. The agent's running helper already
understands the retention-table format, so historical enrollment does not require
restarting an active command. Install compatible migration/helper source and
qualify its native inspection before use. Files outside the two known stdout and
stderr names, PID/recovery identities and arbitrary command workspace files are
not deleted by the trim path.

## Retiring the Mac service-log baselines

The previous inventory's 4,511,020 bytes across 40 baseline copies are not a
permanent exception to the rolling policy. The new explicit migration removes
those redundant/obsolete copies while leaving the existing 1 MiB threshold and
two rolling generations per sink unchanged.

```sh
python3 service_log_retention.py baseline-plan \
  --manifest EXISTING_SERVICE_MANIFEST.json --expected-sha256 MANIFEST_SHA256 \
  --limit 64 > baseline-retirement-plan.json
python3 service_log_retention.py baseline-apply \
  --manifest EXISTING_SERVICE_MANIFEST.json --expected-sha256 MANIFEST_SHA256 \
  --plan baseline-retirement-plan.json --expected-plan-sha256 REVIEWED_PLAN_SHA256
```

Planning creates nothing. It checks the existing manifest binding, source inode,
verified baseline digest, and every retained managed generation. A baseline is
eligible when verified managed rotation covers the rolling tail, or its original
prefix is still present in the unchanged active log and the baseline is only a
redundant copy. Unknown or corrupt coverage blocks deletion. There is no source
log truncation, replacement, signaling or service restart in baseline retirement.

Apply holds the existing global rotator lock and checks exact planned preimages.
It durably writes a `retiring` state containing the specific file's device/inode,
size, hash, manifest and plan hashes before unlinking only `baseline.log`. It then
fsyncs the directory and commits `retired`. Crashes before/after unlink resume
only that exact approved intent. Pending retirement blocks ordinary rotation
until resumed. Once retired, normal rotation validates the persistent source and
retirement fence without requiring or recreating the deleted payload. An
unexpectedly reappearing baseline blocks rotation rather than being adopted.

The small baseline metadata/fence remains. Existing generation slots and active
log paths, inodes, ownership, mode and writers remain unchanged. Older rotator
code that still requires every baseline file must not be used after this state
transition. Install the compatible script before applying a plan; no service
restart is necessary.

## Execution order and current limits

1. Qualify and publish compatible source. Deployment is a separate authorized
   action; this source change does not override any denied gateway deployment.
2. Make the gateway history-evidence endpoint available. Without it, native
   migration fails closed and cannot guess the missing request/time evidence.
3. Plan and enroll native legacy commands while full gateway identity evidence is
   available. Review blocked rows; do not release uncertain work to make it pass.
4. Plan/enroll gateway history. Existing bounded windows can then retire eligible
   bodies while keeping replay and ACK fences.
5. Install the compatible log rotator, plan baseline retirement, and apply the
   exact reviewed plan. Verify normal rotation still works with retired baselines.

This does not make the whole database a fixed-size object: required identity
metadata and genuinely unresolved records remain. It removes the blanket
historical exemption for eligible diagnostic payloads. Enrolling or retiring
records is not part of dry-run preparation, and no live deletion is performed by
the test suite.
