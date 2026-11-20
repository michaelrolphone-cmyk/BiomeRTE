# Pi gateway Store connection-lifetime backport

## Problem and exact target

The running Pi gateway was observed repeatedly deleting a zero-byte
coordination.sqlite-wal and a 32 KiB coordination.sqlite-shm while the small
coordination database remained unchanged. Its Store opens and closes a SQLite
connection for every state read. The idle SessionService maintenance callback
performs several such reads each cycle. On SQLite 3.8.7.1, opening the first WAL
reader can allocate the SHM pages; closing the last connection can checkpoint
and remove both sidefiles. Read-only business behavior therefore still produces
filesystem activity.

This compatibility branch starts at
bcef0dad6f8c745d01603de11919d64547c665ad. Its biomerte/store.py is byte-identical
to the captured installed bounded-stream release:

- Baseline Store SHA-256: 78ac723d35a535003aac587ed3c63869d64f27aea4657017117dc5d507a2f50a
- Baseline Git blob: b0ce6ee452d7ee5bc2dafb1412e6249698eae092
- Existing source-capture receipt: pi-write-attribution-installed-gateway-source-20261120-0414-1
- Gateway entry SHA-256: ed54d1036b96dfbb30fbc59afc382b068fb6ec526ffe4524c93bed33ff4dcc26

The entry script prepends this release root before importing biomerte. Its two
earlier project imports, mcp_events and result_delivery, contain only standard
library imports in the matching Git baseline. Actual installed import identity
remains a host qualification check; a release-directory name alone is not proof.

The alternate PYTHONPATH Store captured from the session-api release also closes
each connection, but it is a different source file and is not the patch target.

## Change

Only biomerte/store.py changes at runtime. The connection lifecycle is copied
from the independently installed native controller at
094858919a6a46e4801f3e4db52d52210fe04ee9:

- Every operation exclusively borrows a connection and starts its own transaction.
- Returned connections are rolled back and leave query_only mode.
- Idle connections are retained with the existing process-wide bound of eight.
  This is a cache bound; it does not limit concurrent operations.
- Failed handles are discarded. Explicit close and garbage collection release
  idle handles. The reentrant pool lock permits finalizer cleanup.
- Connections can move between threads only through exclusive checkout.

WAL, synchronous=FULL, SQLite timeout, schema initialization, record handling,
transaction bodies, authority and session behavior remain unchanged. Durable
database and WAL files stay on their existing filesystem. This patch does not
move replay authority or committed data into memory and does not weaken
uncertainty, fencing, acknowledgment or recovery rules.

This draft targets the exact compatibility baseline, not main. Main already has
newer Store code; merging an old release as a general upgrade is not the purpose.
A live rollout, if qualified and selected by the owner, overlays this one module
onto an isolated copy of the actual gateway release with an independent rollback.

## Qualification

Run, from the isolated exact-source checkout or the actual-release overlay:

```sh
python -m unittest tests.test_store_connection_reuse tests.test_gateway_store_lifetime -v
```

Nine existing native tests cover FULL durability, visibility of external commits,
rollback and query_only reset, concurrent exclusive checkouts, idle-cache eviction,
failed handles, collection of stores and cyclic finalizer reentry.

Three additional tests use real SQLite and disposable files:

1. The old short-lived reader lifetime creates and removes the 32 KiB sidefile
   without changing the main database.
2. The actual idle SessionService path keeps the same warm connection and WAL/SHM
   inodes across repeated cycles, with no record changes or network calls. Closing
   the Store still removes its idle sidefiles normally.
3. An abruptly exiting child leaves committed WAL state and an uncommitted
   transaction. A new process/store recovers the committed record only and remains
   writable, without relying on orderly pool cleanup.

The workflow runs the twelve tests on Linux and macOS. Real Pi Python 3.4.2 /
SQLite 3.8.7.1 qualification is a separate required observation; it is not inferred
from CI. All qualification data belongs in disposable storage. No production
configuration, database or service is changed by this source branch.

## Evidence limits

The observed gateway sidefile writeback is one measured part of SD traffic.
It is not a complete explanation of every device write, journal block, health
snapshot or temporary-file operation. Whole-device improvement must be measured
after any separately authorized deployment. This patch does not change failed
terminal cleanup, worker health publication, or cryptographic temporary files.
