# Supervised command execution

Terminal and Docker adapters run under a detached, per-command custodian.
The controller remains the lease authority; the custodian survives provider
restart and network disconnect without relaunching the adapter.

Before execution, the worker atomically records the command and its recovery
metadata in the existing worker ledger. The runner persists an exact request,
locks custody, and fsyncs a launch marker before starting the adapter.
Concurrent or repeated launch requests cannot execute the adapter twice.
Running status is recorded locally; the provider sends execution heartbeats
and forwards available output checkpoints independently of output changes.

Completion stdout, stderr and exit status remain local until the worker saves
the result and receives an exact controller acknowledgement. A compact pending
index is merged into normal worker dispatch, so results remain recoverable even
when the controller no longer lists a command in its inbox. Lost acknowledgements
are safely redelivered. A durable delivery receipt precedes output cleanup. Compact runner receipts and
launch markers expire with the controller-acknowledged action lifecycle; only
known generated regular files are removed, and unresolved runs are retained.

A late terminal/Docker completion for an abandoned lease records execution_state
and result on that abandoned action. It does not restore the lease, release any
new owner, or emit normal operation success/failure continuation events.

If the custodian itself dies before recording completion, the command remains
uncertain with its recovery directory and last observation. It is never replayed.
This cannot establish arbitrary physical or Docker daemon effects after total
host failure; inspect the retained evidence and daemon state. Existing uncertain
commands without runner records remain subject to their original reconciliation.

Explicit stop and abandoned-generation notices write the adapter stop marker.
Host command limits remain in force; network loss alone does not kill execution.
Firmware relay watchdogs and deduplication remain a separate device concern.

Only host.terminal.run:1 and docker.cli:1 enter the runner automatically. Other
providers retain their established execution path. Existing retention,
acknowledgement hooks, streaming handlers, and per-slot admission remain active.

## Ownership and recovery operations

Supervised terminal/Docker offers advertise execution_custody=true. A missed
heartbeat or offline provider moves executing work into explicit recovery and
retains its guard. An unclaimed fencing timeout remains safely reclaimable.

cap.resource.status reports the current command owner, guard/generation,
last observation, runner state and next recovery action. cap.recovery.list
pages active and historical uncertain work without decoding completed history.
provider.custody records identity-bound runner observations. provider.reconcile
requires an exact command digest and signed executor-quiescence evidence before
settling an interrupted command. A transport timeout is never such evidence.
