# Controller state migration

This utility prepares a single logical site authority from multiple native
controller stores. It does not deploy the authority, start the assistant, change
desired pause/resume state, or edit either source database.

Run source backup operations in the pinned Python 3.11 runtime container on
older hosts. Python 3.4 does not implement SQLite's backup API. A main SQLite
file copied while its WAL is active is not a complete backup.

## Preserve the source before adoption

1. Inventory both stores and arrange independent host recovery before changing
   native site admission.
2. Stop old native binaries that do not implement the source freeze guard.
   Preserve the source home and the complete SQLite database using the backup
   API. The backup includes committed WAL content.
3. Copy each private backup and its manifest to both authorized hosts. Verify the
   actual bytes and canonical record digest on each host. Keep enough disk
   headroom for both backups, chunked checkpoints and the restored runtime.
4. Review the collision plan. Resolve each ambiguous mutable record explicitly.
   Do not combine controller-local event sequences, receipt slots, or request
   identities by choosing the most recently timestamped row.
5. Produce an exact source freeze marker for each final backup, install the
   marker only at that source's native home, and start only the patched native
   runtime. Verify that site writes and site ticks are rejected while local
   maintenance remains available. A marker is not evidence that an earlier
   unpatched process stopped.
6. Export against those exact snapshots, reviewed plan and source markers.
   Bootstrap using the shared chunked checkpoint implementation. Do not send
   whole stores through a 32 MiB RPC body or through chat.
7. Verify both durable replicas, current ownership, original responsibility and
   run/action identities, source fences and current gateway routing before
   enabling ordinary admission. The worker's desired state must remain intact.
8. Retain the source backups and migration provenance. After new work commits,
   rollback must recover the latest authoritative checkpoint and generation
   floors; it must not reopen an older source snapshot for writing.

All files created by this utility are private, use exclusive creation, and
refuse to overwrite an earlier artifact. Source backup, inventory and snapshot
serialization use bounded row-sized memory. Perform the merge/export on the
host with adequate memory; values are shared by reference and JSON output and
hashing are streamed. Source payloads remain on authorized hosts.

## Commands

Create a WAL-safe backup and a manifest. This is read-only with respect to the
source database:

    python3 deploy/controller-authority/migrate.py backup \
      --home /private/source-home \
      --output /private/migration/pi.sqlite \
      --manifest /private/migration/pi-backup.json

The manifest includes the backup filename, byte hash, canonical record digest,
per-kind counts and state counts. Keep the copied manifest beside the unchanged
backup filename. The planner accepts those manifests directly, avoiding a
second full JSON snapshot on the small host.

A target file declares a separately provisioned logical identity:

    {
      "service_id": "controller-runtime",
      "authority_id": "<new logical authority fingerprint>",
      "site": "<existing site>",
      "controller_aliases": ["<pi fingerprint>", "<mac fingerprint>"]
    }

Physical controller identity cloning is rejected. Both original controller IDs
are signed routing aliases; they are not independent write authorities.

A policy can retire a proven inactive secondary event domain and explicitly
select a source for individual ambiguous records:

    {
      "retire_event_sources": ["<mac fingerprint>"],
      "resolutions": [
        {"kind": "definitions", "id": "<exact process>", "source": "<pi fingerprint>"}
      ]
    }

Event-domain retirement is rejected if that source has subscriptions, waits,
nonterminal runs or assistant execution records. Exact original events,
counters and displaced values remain in the provenance archive. Do not retire
an active domain simply to make a plan pass.

    python3 deploy/controller-authority/migrate.py plan \
      --source /private/migration/pi-backup.json \
      --source /private/migration/mac-backup.json \
      --baseline "<pi fingerprint>" \
      --target /private/migration/target.json \
      --migration-id controller-runtime-20261120 \
      --policy /private/migration/policy.json \
      --output /private/migration/plan.json

The plan includes complete source inventories and hashes, deterministic
collision decisions and unresolved blockers. It contains no record payloads.
Recreate a new plan artifact after reviewing new explicit resolutions.

    python3 deploy/controller-authority/migrate.py marker \
      --source /private/migration/pi-backup.json \
      --target /private/migration/target.json \
      --migration-id controller-runtime-20261120 \
      --output /private/migration/pi-controller-authority.json

Repeat marker preparation for the other source. Install and verify source
fencing using the deployment procedure, then export:

    python3 deploy/controller-authority/migrate.py export \
      --source /private/migration/pi-backup.json \
      --source /private/migration/mac-backup.json \
      --plan /private/migration/plan.json \
      --marker /private/migration/pi-controller-authority.json \
      --marker /private/migration/mac-controller-authority.json \
      --output /private/migration/bootstrap-state.json

Export rejects a changed source, modified plan, unresolved collision, missing
marker or marker bound to a different source digest. Its output contains state
and authorization for CheckpointStore.bootstrap; use the runtime's chunking
layer for distribution.

## Merge rules

- Equal and disjoint records retain their exact identities and values.
- Content-addressed artifact bytes must agree with their digest. Durable upload
  lifetime wins over a transient copy of the same bytes.
- Document heads are reconstructed from retained revision DAGs. Missing head
  revisions block the plan; no new revision is fabricated.
- Writer and physical generation floors only increase.
- Equivalent ordinary receipts coalesce. Ambiguous ordinary receipts retain
  both originals and receive a durable _controller_receipt_conflicts marker;
  native command admission must reject replay independently of ordinary receipt
  retention.
- Ordered receipts preserve the greatest sequence. Different results at the
  same greatest sequence receive a _controller_receipt_stream_conflicts
  marker and have no replayable result. Admission rejects requests at or below
  that conflict floor.
- Action, lease, run, work, provider, definition and other mutable collisions
  need exact source choices. Every displaced original is preserved in
  _controller_migration_archive.
- Retiring a secondary event domain preserves its original events, delegation
  history and local cursor metadata in the archive. It does not emit those
  historical events into the primary's active event stream.
- Migration does not declare uncertain effects successful, assign replacement
  operation IDs, resume a paused assistant, or reset fencing generations.

The durable conflict checks must be deployed with this migration. A tombstone
alone is insufficient because ordinary receipt retention can later remove it.

## Tests

Run the focused suite in the physical development workspace:

    python3 -m unittest discover -s tests -p test_controller_state_migration.py

Cases cover WAL-safe backup, source immutability, streaming snapshot/hash
equivalence, preservation of paused state and source records, explicit mutable
conflicts, event-domain blockers, receipt conflict quarantine, monotonic floors,
DAG reconstruction, exact marker/plan binding and private exclusive outputs.
Runtime admission and two-host takeover tests remain separate requirements.
