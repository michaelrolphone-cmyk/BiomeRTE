# Mac consumer delivery recovery — 2026-11-05

One uncertain terminal slot caused Worker.once to abort before dispatching any other slot. The controller transport remained healthy, but consumers waited indefinitely for their terminal commands to be claimed. The af-install-v4 controller record had already been reconciled as failed and quiescent; the worker SQLite receipt remained uncertain.

The worker now isolates activation, fence, and action failures by guard. Independent slots keep dispatching. It reconciles local uncertainty only against an exact controller operation identity, provider, guard, and an explicitly quiescent terminal result. Prior uncertainty evidence is retained in the local reconciliations table. Unresolved same-slot work cannot be bypassed or replayed. Capacity remains 16 on the Mac terminal, with no new job timeouts.

worker-health.json records current execution degradation separately from transport health.json. This local file is not itself a controller-wide or assistant success indicator.

Validation: 39 native Python tests passed, including cross-slot progress under failed fences, local receipt reconciliation, mismatch rejection, and unresolved effect protection. Deployed the worker module to the Mac terminal runtime with a source and SQLite checkpoint, then restarted only the terminal worker through its existing launchd supervision.

Verified consumer results:
- af-finish-inspect-mac-1: executed=true, quiescent=true, exit_code=0.
- assistant-repair-inspect-host-20261105: executed=true, quiescent=true, exit_code=0.
- consumer-repair-terminal-native-20261105: fresh lease bound, command claimed and executed on Darwin arm64, exit_code=0.

Both original inspection scripts also printed a git error because their target deployment directories are not Git checkouts; successful terminal delivery does not establish native test or scientific qualification.

Cancelled install-independent-maintenance-20261105 while it was still queued and never claimed. It was superseded by the confirmation-enforcing installer. No uncertain command was relabelled successful. Slots 8 and 10 still require reconciliation of older effects; they no longer block healthy slots.

Related assistant dispatch and structured-output repair was merged separately in PR #29; it must be tested and deployed before claiming assistant work recovered.
