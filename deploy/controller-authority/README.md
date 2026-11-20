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
2. Stop old native binaries and gateway event-delivery agents that do not
   implement the source freeze guard. Preserve the source home, complete site
   SQLite database and gateway event-delivery SQLite sidecar using the backup
   API. Each backup includes its committed WAL content. Freeze both writers
   before the final pair of backups; separate live backups are not an atomic
   cross-database snapshot.
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
      --events-database /private/gateway-home/events.sqlite \
      --output /private/migration/pi.sqlite \
      --manifest /private/migration/pi-backup.json

The manifest includes the backup filename, byte hash, canonical record digest,
per-kind counts and state counts. Keep the copied manifest beside the unchanged
backup filename. The planner accepts those manifests directly, avoiding a
second full JSON snapshot on the small host. With --events-database, it also
creates pi.events.sqlite beside the site backup and binds that sidecar's byte
hash, canonical digest and table counts. Locate the actual native gateway
--key directory: its events.sqlite is a different database from site.sqlite.
Do not read or print the key file. The sidecar path must be observed, not guessed.

The plan requires an explicit observation of subscriptions, receiver_receipts
and failed_events for every source. An omitted sidecar is a blocker, not proof
of zero receivers. The snapshot command is a site-only diagnostic and cannot
produce an admissible migration plan by itself. Keep the manifest and both
unchanged SQLite filenames in one private directory.

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

For the actual helper bootstrap, replace the export command with bootstrap and
add --config /private/migration/staging-helper.json. Use a fresh checkpoint_db
path in that private configuration, the final logical service scope and the
same checkpoint key that both helpers will use. This command writes each
kind/hash-prefix shard directly into the staging checkpoint chunk store, then
signs a small root manifest. The --output file is the root packet; the adjacent
checkpoint SQLite and its .chunks directory contain the durable payloads.
Transfer and verify the complete staging store on both hosts. The bootstrap
command rejects an existing checkpoint path and never modifies a live helper.

Export and bootstrap reject a changed source, modified plan, unresolved collision, missing
marker or marker bound to a different source digest. Its output contains state
and authorization for CheckpointStore.bootstrap; use the runtime's chunking
layer for distribution.

## Freeze and verify through independent maintenance

The source-fence installer closes the hash-to-marker race by holding SQLite
BEGIN IMMEDIATE while streaming the current canonical digest and atomically
installing the complete marker. Patched native stores recheck their admission
guard after obtaining that same write lock. No canonical database row changes.

    python3 deploy/controller-authority/deploy.py freeze \
      --home /private/source-home \
      --marker /private/migration/source-controller-authority.json \
      --receipt /private/migration/source-marker-installed.json

An earlier unpatched process must already be stopped; this utility cannot make
it honor a guard it does not implement. The receipt therefore explicitly says
runtime_admission_verified=false. Verify actual guarded native calls separately. Stop the old native gateway
agents before the final event sidecar backup, and restart only code that applies
the source marker to site event subscriptions, Grok mutations and delivery.
Local subscriptions remain with the native host.

On each host, verify the independently observed root reference and every
content-addressed block/shard before admission or takeover qualification:

    python3 deploy/controller-authority/deploy.py verify-replica \
      --config /private/helper/service.json \
      --expected /private/migration/current-checkpoint-reference.json \
      --receipt /private/migration/replica-verified.json

The expected input is either an exact service_id/sequence/digest reference or
the initial signed root packet. Verification authenticates scope and signature,
checks the expected current head, reads all blocks, verifies each shard digest
and canonical kind/ID/prefix ordering, and reports only counts/hashes. It never
installs, advances, restores or starts the authority.

Visible unfinished session reservations, ownership handoffs or external
pending request receipts block migration. Also inspect and back up the source
coordination sidecar through independent maintenance; a site-only inventory
does not prove that sidecar has no undelivered packages.

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
  history and local cursor metadata. Indexed _controller_source_events and
  _controller_source_delegation_events retain each alias's original history;
  _controller_source_feeds binds its old high-water mark and retention floor to
  the adopted logical high-water mark. Runtime alias feeds continue old numeric
  cursors and translate future logical sequence numbers. Historical secondary
  events are not emitted into the primary's active event stream.
- Native gateway subscriptions, receiver receipts and failed deliveries retain
  every original SQL column, including callback signing keys, owner/grant,
  generation, expiry, cursor and retry state. Canonical identities include the
  original controller alias, so equal native IDs from two sources remain
  distinct. Site receivers move to the logical runtime without registration or
  challenge replay. Identifiable local receiver state remains native and is
  also retained in provenance.
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
DAG reconstruction, exact marker/plan binding, event sidecar byte binding,
source-alias cursor continuity, exact receiver-column preservation and private
exclusive outputs.
Runtime admission and two-host takeover tests remain separate requirements.
