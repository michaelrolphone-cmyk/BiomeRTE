# Config-only bootstrap to maintain transition

No live action is performed by this document or patch.

## Gate and exact plan

The parent first enables future-only gateway retention with a fixed sequence boundary greater than the head observed when that boundary is chosen. Preserve managed uploads and the existing audience. Verify successful deployment and effective environment. Do not lower the boundary later or include historical deletion.

Only after that verification, prepare each native host's separate frozen plan using:
- files: []; manifest_edits: [].
- phases: gatewaylink only, reusing that host's verified link-only snapshot/restart/health helpers.
- One config_edits item: phase gatewaylink, target the verified node.json, path [gateway_journal, checkpoint_mode], old_present true, old bootstrap, new maintain.
- old_sha256 and unchanged_files[target] both equal the freshly observed full node.json hash.
- Existing protected service and configuration baselines from fresh host observation.
- gateway_retention_activation: enabled true; deployment_status success; exact verified deployment_id; positive environment_revision; observed_head; from_sequence strictly greater than observed_head.

The activation receipt is an explicit parent attestation frozen into the plan, not an independent live gateway query. Missing/invalid receipt blocks both preparation and apply. Native maintain calls also fail closed with enabled:false if the gateway flag is not enabled. The receipt is not authorization to change any gateway setting.

Prepare a new private bundle with only this change; qualify on each host's own Python/SQLite runtime. Do not reuse an old plan/preimage. Follow existing exact plan/runner/helper hash checks.

## Application sequence

After parent approval to apply, apply gatewaylink only on one host, reconcile its receipt, then the other. The runner checks protected states and node hash before writing and checks the hash again immediately before replacement. It saves the exact original bytes to mode0600 backup, preserves target mode/owner, atomically replaces only the config, and restarts only the gateway link. Other JSON values are preserved; whitespace formatting may change.

Existing health and protected-state checks must pass. Failure restores exact original bytes/mode/owner, restarts the link, and verifies rollback health/protected states. Reconcile any uncertain outcome before proceeding. No controller or terminal restart, submission hold, enrollment, data purge or production database read is needed.

While the prior checkpoint remains below the boundary, compaction removes no authoritative journal records. Once it passes the boundary, only records at/above that boundary and below all recovery/read-pin floors become eligible. All older history remains protected.

## Qualification

New tests cover missing/invalid activation evidence, non-bootstrap preimage, mixed phases/files, wrong key/hash, successful owner/mode-preserving config-only apply, private backup, stale preimage refusal without restart, and health-failure rollback. Existing protected-service and native helper tests remain applicable.
