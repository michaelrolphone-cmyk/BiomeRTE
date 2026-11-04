# Biome assistant workflow delivery

Prepared for review and later installation. Nothing in this package has been imported into, registered with, or started on the live Biome.

The design is now bound to the native `urn:biomerte:v1` executable profile. It includes 50 individually importable BPMN processes, actual operation implementations, prompts and response schemas, deterministic gateway code, input/output mappings, and durable waiting/error behavior. This is authored delivery source, not a claim of production qualification.

## What to review

| File | Purpose |
|---|---|
| `biome-bpmn-atlas.html` | Visual process atlas; select a step to inspect its binding, prompt, schema and branch mappings. |
| `bpmn/*.bpmn` | The 50 executable processes. Import individually, in `import-order.json` order. |
| `biome-improvement-design.bpmn` | Combined review XML. The native importer accepts one process per call, so use the individual files for installation. |
| `assistant_support.py` | Internal controller operations, protected provider submission, scoped records, admission/finalization transactions and continuation coordination. |
| `assistant_routing.py` | Every domain gateway's evaluable selector. No prose predicate is executed. |
| `assistant_catalog.json` | All modeled stage bindings, complete prompts, response schemas, terminal dispositions and continuation targets. |
| `controller-integration.patch` | Small prepared engine integration: verified workflow identity, exact-token durable waits and tick coordination. |
| `native-task-bindings.csv` | Every generated native service-task binding, including mapping, route and fault plumbing. |
| `bindings.json`, `conditions.json`, `data-mappings.json` | Business-stage bindings, JSON conditions and all 93 call mappings. |
| `schemas/` | Response, review, outcome, action/proposal, deployment and task input schemas. |
| `assistant_workspaces.py`, `assistant_workspace_runner.py` | Workspace/Git orchestration, fenced access, complete source snapshots, isolated builds and exact reviewed commits. |
| `workspace-contracts.json`, `capability-observation.json`, `gitlab-mcp-catalog.json` | Deployed argument schemas, observed ready providers and the SHA-256 verified catalog of 37 native GitLab MCP tools. |
| `workspace-bindings.json`, `coding-task-example.json` | Concrete provider bindings and an example coding request. |
| `tools/container_build_test.py` | Retained legacy standalone Docker runner; the updated coding flow uses the run workspace. |
| `tools/prepare_install.py` | Offline source/configuration preflight and deployment-plan creation. Its default mode writes no controller state. |
| `tests/test_delivery.py` | Tests against the actual pinned Biome engine and transaction store, in isolated temporary homes. |
| `reliability-qualification.md` | Repeatable production qualification scenarios, with explicit observables and pass/fail criteria. |
| `provenance.json` | Source pin, capability observations, generated-file digests and validation boundaries. |

## Existing capabilities and new delivery code

No new capability-registry types are introduced by this package. Reasoning uses `agent.lmstudio.delegate:1`; source review uses `agent.chatgpt.delegate:1`; decisions use `human.decision:1`; coding uses the deployed `workspace.*:1` contracts and `git.mcp.tools:1` / `git.mcp.call:1`. Controller configuration still uses `host.terminal.run:1`; unrelated existing Docker, storage and source capabilities remain available. The live GitLab catalog contains 37 native tools, including `save_merge_request`. No new capability-registry type is required.

`assistant.*` operations in the Python module are internal controller glue delivered here as source. They must be reviewed and installed together with the engine patch. Marking the BPMN executable does not remove that installation requirement. The existing authorization and provider implementations remain the enforcing boundary.

The source baseline is BiomeRTE commit `af4220d6cee3cbd51b51b6db13aad1eaaeafa7fc`. The deployment preflight refuses source drift. Rebase and rerun qualification if the controller has changed; do not overwrite newer code with the fixture.

## Runtime behavior

A standing responsibility owns finite cycle permits. Admission, trigger deduplication and start outbox are one store transaction. Finalization consumes the permit and atomically records the checkpoint plus exactly one successor or event/timer wait obligation. Replaying either transaction returns its original receipt.

Cycles launch observation, progress assessment and episode-dispatch work independently. They do not wait for a human answer or a model response. Runnable work is filtered by reservations and accepted dependencies; blocked backlog does not cause immediate successor loops. The observation cadence is an explicit deployment setting, not a task timeout. Event waits preserve the cycle's observation watermark so work arriving during finalization is not lost.

Inside an episode, pending provider submission retains the original workflow revision, call stack, token, operation ID and proposal. The engine parks that token; inference capacity belongs only to the actual provider call. Restart reloads the same token. No resubmitted workflow, inferred success or silent approval is used to continue. Failed, uncertain, revoked and malformed results have separate paths. An unknown external effect stops ordinary action and requires reconciliation.

Every external mutation, sensitive read, Docker invocation and source/context disclosure uses a concrete proposal and the existing authorization guard. Authority is checked again by the provider boundary. The installed scope allows only assistant protocol bookkeeping, local reasoning transport and decision transport; it does not authorize arbitrary application effects. Canonical action descriptors record rejection independently of question wording. Human text remains attributable input; conditional approval requires clarification through the existing authorization implementation.

Governance classifies effects before deciding depth. New external effects receive architecture comparison and the sequential system checklist. An incomplete system/dependency inventory returns unresolved. Model recommendations remain advisory. Deterministic verification uses declared measurement mappings and criteria; an LLM's confidence never marks tests or safety checks passed.

Build/test work uses `workspace.exec:1` in the same run-owned Docker environment as source editing. The runner snapshots every tracked and nonignored file, including binary bytes, executable bits and symlink targets, then tests a disposable copy. Test failure is structured evidence and consumes a coding attempt; provider/transport failure is unresolved and consumes none. It retains commands, stdout, stderr and exit status. Final ChatGPT sign-off must name that same digest and passing test evidence. The policy is five local attempts per feedback round and at most three feedback rounds. Protected-core integration requires the configured current verifier, outside the changed component set, plus qualification and human authorization.

Supervision and recovery are separate finite processes. The external watchdog must run on a qualified independent Biome controller/peer with its own available health-observation and recovery path. It uses a real protected health probe and exact preauthorized recovery proposal. It cannot recover a dead controller by calling that same controller. The package does not create a second physical controller or establish replication by itself.

## Deployment inputs

`deployment-inputs.json` deliberately contains unset installation choices. These are deployment parameters, not missing capability implementations: actual controller home, Python path, effective model context budget, complete system inventory, trusted verifier revision and observation policy. Workspace/Git provider IDs, canonical GitLab URL and the qualified template defaults are now populated from the deployed implementation. The runner is embedded in the workspace command and needs no Mac host bind path. Preflight rejects missing values and `operator_approved:false`.

A task supplies its concrete goal/measurement criteria, repository/source references, tool action and verification probes. Versioned task inputs are read from `assistant.task.<task_id>` through the existing Biome knowledge store; conflicting heads stop admission, and the exact revision enters evidence. A coding task supplies a credential-free GitLab URL/project, full base commit, dedicated output branch, optional workspace template/resource requirements and argv build/test commands. `linux-basic@1` is the default; `esp-idf@2` supplies ESP-IDF 5.5.1 and QEMU tools under amd64 emulation. An optional `git_mcp_request` pins the current tool input-schema digest and its exact arguments. The example uses the observed `save_merge_request` tool; creation requires project, title, source branch and target branch. Never repeat an uncertain create operation; reconcile by the original receipt and remote branch/MR identity. A qualification task supplies its isolated test action and independent verifier action. Actions are validated against the real contract input schema before authorization. There is no universal build command invented for an unknown repository.

Initial configuration explicitly provisions bookkeeping and transport authority. Registering a responsibility starts it paused. An authenticated operator resume is a separate action. Importing the models alone starts nothing.

## Reproduce local validation

Run from this directory, with Python 3.11+ and OpenSSL available:

```sh
python3 build_delivery.py
python3 tests/test_delivery.py
python3 tests/test_workspaces.py
python3 finish_package.py
```

The fixture is generated from the exact vendored source plus the prepared patch. Tests register fake providers only inside temporary local stores. They do not contact the live Biome, GitLab, Android, Docker or LM Studio.

Local native-engine tests establish executable structure, typed outcome propagation, transaction rollback, duplicate dispatch, revocation, pause/resume, persisted continuations, exact source/test/review mapping and idle cycle finalization. They do not establish physical controller failover, production Android delivery, real model output quality or host recovery. Those live scenarios are prepared in `reliability-qualification.md` and remain to be run after deployment is authorized.

## Run-owned workspaces and Git publication

Revision 5 adds `workspace_open`, `workspace_transfer`, `workspace_park`, `workspace_resume` and `git_publish`. The coding loop provisions once, reacquires before each coding turn, suspends before external review, and publishes the reviewed commit before its existing change-management/release gate. The five-attempt / three-feedback-round policy is unchanged.

The workspace belongs to the independent **parent episode run**, including its nested subprocesses. It does not belong to a short standing-responsibility cycle, provisioning subprocess or agent turn. Each subprocess inherits the owner ID; ancestry is checked before accepting it. State carries the workspace ID, resource, holder, generation, template/platform, checkpoint, source snapshot and publication receipts. The next writer uses `workspace.handoff:1` and replaces its generation from the returned receipt. Direct parallel branches require separate workspace names; no concurrent read/write sharing mode is claimed.

Template images build on first use and are reused by exact revision/platform. Acquisition restores paired image/volume archives and performs the provider's authenticated GitLab probe. Mac is the current binding; unavailability waits rather than switching to a Pi. `workspace.git:1` operates on `/workspace/source`. Existing `git.cli:1` and convenience operations use separate host namespaces and must not be substituted for workspace source operations. The provider injects the existing non-admin service credential into tmpfs and restores canonical hostname/TLS routing; no token belongs in task inputs, model context, templates or checkpoints.

Suspension is explicit before review or at a chosen safe handoff/wait; the existing monitor also handles native quiescent human/message/timer waits. Active delegated work is not automatically stopped. A pending concrete command or authorization retains its exact descriptor; changing its generation requires a fresh reviewed intent, not silent replay. Unknown/faulted/deferred workspace outcomes keep the token and ancestor episode running with a `workspace_resolution` continuation, so the terminal-state monitor cannot delete recoverable state. Reconcile the original provider receipt before continuation; explicit cancellation remains a terminal action and permits cleanup. No wall-clock task cutoff is introduced.

Publication rechecks the full source digest against passing tests and the final review, creates a commit, pushes that exact SHA to the dedicated branch, and verifies it with `ls-remote`. The receipt stores URL, branch, commit, source digest and operation evidence outside the container. Optional GitLab review requests re-read the actual MCP catalog and reject schema drift or tool errors. This publication does not replace the existing protected change-management gate or authorize deployment/merge.

Source publication is implemented. Other desired build files need explicit verified publication through an available storage/artifact action before the run is ended; there is no automatic arbitrary-artifact uploader. Private workspace files, checkpoints, volumes and images are disposable on terminal completion/failure/cancellation after children finish. Shared template images and GitLab commits remain. Cross-host archive replication, Pi execution, per-run GitLab credentials, emulator firmware boot and physical flashing remain outside the deployed provider qualification.

## Recovery and validation scope

The revision-4 archive and every included provenance hash were intact; all 26 recovered tests passed. Revision 5 updates models, native BPMN, controller glue, prompts, input schemas, mappings, configuration, atlas and delivery plan. The workspace/Git providers are already deployed and separately qualified; **the revised proactive assistant package has not been installed or live-run**. Offline tests exercise the native engine, nested process ownership, suspended recovery, fencing, schema drift, exact source publication and uncertain receipts. Live qualification scenarios remain in `reliability-qualification.md`.
