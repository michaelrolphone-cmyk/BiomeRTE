# Durable publication of uncertain command evidence

A frozen terminal runner can remain uncertain after its custodian disappears or
after it finishes without a valid result. The worker must retain that uncertainty
and inspect the original runner for later evidence.

Previously, each retry sent new request IDs for unchanged custody and uncertain
results, rewrote the local result, and made the controller remove recovery
markers. A subsequent tick reinstated those markers. This produced new receipts,
action writes and recovery events without learning anything about execution.

## Changes

The supervised worker gives final, non-running custody snapshots and uncertain
results deterministic request IDs binding the full action identity, operation
name and exact transmitted arguments. The existing durable API receipt handles
lost responses and process restarts. Changed evidence gets a different identity.
Live starting/launching/running custody traffic retains its existing behavior.
The worker also leaves an identical local result row unchanged.

The controller records the first provider uncertainty report, even if a lifecycle
timeout already marked the action uncertain with the same payload. Exact later
uncertain reports leave action/recovery state and events unchanged after normal
provider, generation and current-guard checks. Changed uncertain evidence is
recorded but cannot resolve recovery. Only a genuine settled outcome can do so.

Repeated frozen custody observations keep the original observation time. A repeat
of old executor evidence is not evidence of a newly live custodian. Changed
observations still commit; live launching/running observations still renew the
watch. Provider transport availability is separate from executor evidence.

No uncertain command is acknowledged as finally delivered, cleared, released,
reassigned or replayed. The original pending record and runner evidence remain.
A later valid completion can still be delivered and retired through the existing
final acknowledgement path.

## Mixed-version qualification

The installed terminal directory name is not a source version. The source capture
from 2026-11-20 verified these actual files:

- supervised_commands.py: SHA2561992e93d17bd9fffdad89f541ffd4ab948dbf7599a534d1e5ac0f2871a35807d
- worker.py: SHA256acdc1e715c3c2757540c287cb53d407b179db6811fa168cb5916678df8821dde
- command_runner.py: SHA256116334c1320a306350ec4f13077aa2ffcbf3d22794d1427c1475ac2e32768fc5

The installed supervised_commands.py exactly matches this change's parent.
The minimum client rollout changes only that file. The controller-side semantic
changes can be qualified and deployed separately. The existing API receipt code
is identical in controller094858919a6a46e4801f3e4db52d52210fe04ee9 and this parent.

Run the client tests in a disposable checkout containing the unchanged controller,
captured installed worker and command runner, and the patched supervised module:

    python -m unittest tests.test_supervised_publication -v

These tests exercise the real API/SQLite receipt handling through an in-process
transport fixture; they require no external connection or production settings.
They cover interrupted custody, finished-but-invalid output, first delivery,
changed evidence, later completion, lost responses, worker/controller reopening,
receipt retirement, and process exit after durable acceptance.

The client-only repair relies on existing receipt retention. If an old controller
eventually removes a receipt, the same evidence can be accepted once again; the
controller-side semantic no-ops remove that residual repeated-state mutation.
A retired receipt returning410 is never treated as a fabricated acknowledgement.

Run the complete focused suite with:

    python -m unittest tests.test_supervised_publication tests.test_uncertain_result_idempotence tests.test_supervised_commands tests.test_supervised_authoritative_recovery tests.test_resource_recovery tests.test_worker_recovery -v

This source change does not merge main, deploy services, restart controllers, or
change unit KillMode. Qualification and live rollout require separately recorded
exact source pins and recovery receipts.
