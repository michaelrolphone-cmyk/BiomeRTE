# Operational qualification

No live qualification was performed. Execute these scenarios only in an authorized isolated realm/namespace before production adoption. Use the exact delivered process revisions, current controller source, real provider generations and Android enrollment. Record original request IDs, process revisions, token IDs, grants, operation IDs, provider receipts and finalization receipts. Do not use inferred completion or substitute an unrelated successful action.

| Scenario | Injected condition | Required evidence and pass condition |
|---|---|---|
| Real model selection and schema | Submit every delivered phase prompt through LM Studio. Include malformed/partial response and an unavailable selected model. | Actual model is one of configured aliases; only schema-valid JSON advances. Unsupported output preserves feedback and stops/escalates without tool execution. Capture useful action-selection quality separately from schema validity. |
| Restart while awaiting Android | Stop controller after decision submission and before answer; restart, then answer on enrolled Android. | Exactly one decision request/revision, original token and operation; no approval before the authenticated answer; response correlated to exact proposal. |
| Lost provider response | Disconnect after provider claims a harmless test action but before result delivery. | Original operation remains uncertain/pending. No new operation executes the same effect. Reconciliation uses original receipt; unresolved effects stay unresolved. |
| Duplicate delivery | Deliver the same work trigger, admission outbox item, capability request and provider result twice. | One logical cycle admission, one external effect, one grant consumption and stable result receipt. Conflicting duplicate arguments are rejected. |
| Revocation after reservation | Revoke a grant after reservation, before dispatch/claim, using the enrolled approval/control path. | Actual provider guard refuses execution; refusal is attributable; no effect appears on target. Repeat with a queued lease promoted after revocation. |
| Pause/resume | Pause with an active cycle and a separate pending episode. Restart, then resume. | Desired pause survives restart; no new admissions; existing pending operation retains identity. Resume creates or coalesces one admission. Pause does not imply cancellation of an already dispatched effect. |
| Finalization crash windows | Crash before commit; after checkpoint/outbox commit; after start materialization but before receipt observation. | Either the old permit remains, or exactly one committed successor/wait exists. Replaying returns original logical cycle ID. No checkpoint-only state or competing successor. |
| Racing wake | Deliver new eligible work between cycle summary and finalization. | Event watermark causes a wake. No lost work and no blocked-backlog spin. |
| Real controller failover | While an action and a cycle are pending, remove the primary; let the configured secondary take over, then restore primary. | Shared authoritative checkpoint, original action IDs, monotonically fenced provider generation, one current cycle permit and one successor. Returning primary cannot act with stale authority. This requires actual replication/fencing, not two local processes over unrelated databases. |
| External watchdog isolation | Stop core controller and its host capability service. Observe and recover from the independent peer. Also remove peer authority. | Health probe and recovery still function without core routing. Only the exact standing recovery action executes. Missing/revoked authority produces pending/unresolved, never a self-issued grant. |
| Harm plus blocked recovery | Deliberately fail a reversible isolated safety criterion; reject or defer the proposed recovery on Android. | Harm flag, distinct rejection/defer outcome and affected-system evidence survive all caller mappings. Ordinary improvements stop; no goal-unmet retry on a harmed system. |
| Model/image contention | Request inference and image generation through the existing shared resource arbiter. | One resident front-runner according to the deployed policy; other request queued; model identity and original operation preserved. No second arbiter is introduced by this package. |
| Coding review | Fail tests for five attempts; provide feedback; exercise three feedback rounds. Also request sign-off for a stale revision. | Escalation follows policy; no failing or stale candidate releases; exact passing source digest is independently reviewed. |
| Protected-core change | Propose an authorization/guard/verifier change. | Current independent verifier remains pinned outside candidate modifications; isolated qualification and Android authorization precede activation. Candidate cannot approve itself. |
| Long-running operation | Run a useful authorized build/download longer than prior removed limits. | No assistant-imposed elapsed/CPU timeout. Durable progress/result arrives under original identity, with only actual host/user resource policies applied. |

A live test harness must retain credentials outside this package and must not intentionally power off real hosts or damage application data without the concrete authorization for that scenario. The isolated native-engine suite provides automated local fault injection; the table identifies the additional physical-provider qualification boundary.


## Revision 5: workspace and Git integration qualification

These are prepared scenarios, not a claim of live execution of the revised assistant. Prior provider qualification is in workspace-deployed.json; current ready bindings are in capability-observation.json.

1. Start an independent episode calling development and workspace_open. All nested calls inherit the parent episode owner. Finishing workspace_open or a short standing cycle must not dispose the episode workspace.
2. Use linux-basic@1 and esp-idf@2 twice each. Verify image reuse by exact revision/platform, isolated run volumes, real SDK/QEMU probes and project build.
3. Handoff to another acting agent. Reject stale holder/generation writes. Preserve files, installed tools, source pin, checkpoint and next action. Separate workspaces for parallel writers.
4. Suspend for review, restart controller/provider, reacquire the same instance with refreshed fencing. Verify source identity, canonical TLS routing and authenticated GitLab connectivity. Exclude secrets from context/checkpoints.
5. Fail a build: one completed attempt and exact failed evidence. Lose a provider receipt: no attempt consumed, duplicate effect or terminal cleanup; reconcile the original operation while its token stays pending.
6. After exact-source sign-off, change a tracked/untracked/binary file, executable bit or symlink. Publication must reject the changed digest before commit/push.
7. Publish the reviewed commit to a disposable branch, verify remote SHA, retain its receipt outside the workspace. A refused push or mismatched remote must block completion.
8. Discover actual MCP schemas and use save_merge_request. Reject schema drift/tool errors. Lose a creation receipt and reconcile the original operation/remote identity without creating a duplicate MR.
9. Publish every desired output before ending the owning episode and its children. Verify retried disposal of private containers, volumes, snapshots and images; retain shared templates and Git commits. Cancellation permits discard, not successful delivery.
10. Make Mac unavailable: remain pending on the pinned provider, without claiming Pi/cross-host recovery.

Offline tests use the native engine/stores with fake providers plus real local Git repositories. Revised full assistant execution, physical failover, Android/model delivery and live fault injection remain unexecuted until deployment is requested.
