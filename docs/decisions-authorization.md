# Biome Decisions and reusable BPMN authorization

Biome owns the phone enrollment, questions, original signed responses, proposal history, grants, revocations, reservations and execution results. The Android app caches its inbox, drafts and durable reply outbox encrypted with an Android Keystore AES key. An independent Keystore RSA key signs phone interactions. No Firebase, Humus, Rhizome, Mycelium, or external decision service participates in this runtime path.

The app is a separate install, `org.biomerte.decisions`, named **Biome Decisions**. Its widget preserves the previous Decisions colors, continuous question/options list, direct buttons for every choice, optional written reply, and large checkmark / **No decisions** empty state. Locked phones hide questions and answer buttons. A locally queued reply remains visibly pending until the controller acknowledges it. Network errors preserve the original signed envelope and operation ID; one failed reply cannot block refresh of other questions.

## Install and pair

Build `android/decisions` with JDK 17, Gradle 8.9 and Android SDK 35, or its Dockerfile. `gradle testDebugUnitTest assembleDebug` builds the signed debug delivery APK. Keep the signing keystore for compatible future updates; production delivery must consistently reuse that key.

Deploy the additive controller modules and gateway route `/api/decisions`, and import `examples/biome-authorization.bpmn` as `biome-authorization` using `bpmn.import`. The controller seeds `human.decision:1` on `human.decisions` with unbounded *pending question* capacity. This represents a durable inbox, not automatic human availability. A disconnected phone never approves anything.

An owner invokes `decision.pair` with a label and saves the returned JSON to the phone. Pairing codes are single-use and can be revoked by closing the pairing or revoking the enrolled phone with `decision.device.revoke`. Add the private Site's `gate_token` to the pairing bundle when required by its hosting gate. The app imports the JSON through **Choose pairing JSON**; it never stores the redeemed pairing code. Enrollment is bound to the generated public key and installation ID. Retries can only recover the same enrollment. Phone requests cannot choose arbitrary controller methods or realms.

## General human decisions

Use ordinary `cap.acquire` / `cap.invoke` / `cap.status` (or `cap.submit`) with contract `human.decision:1`, resource `human.decisions`, and:

```json
{"request":{"id":"stable-question-id","workflow_instance_id":"caller-run-id","proposal_revision":1,"title":"Choose an option","question":"Which option should this workflow use?","choices":[{"id":"first","label":"First option","meaning":"use_first"},{"id":"later","label":"Defer","meaning":"defer"}],"supporting":{}}}
```

The returned `result.decision` includes the exact question digest, request/revision, choice, original text, explicit outcome, enrolled device ID, receipt ID and signed envelope. A general decision is not an authorization. Repeated signed submissions return the same receipt. Different replies after settlement are rejected and remain preserved on the phone for review. Changed question content requires a new proposal revision.

## Reusable permission subprocess

Both JSON process graphs and BPMN call activities can call `biome-authorization`. Pass:

```json
{
  "request_id":"stable-permission-id",
  "workflow_instance_id":"actual-calling-run-id",
  "question":"May I perform this concrete action?",
  "proposal":{
    "revision":1,
    "action":{"contract":"core.echo:1","resource":"builtin.echo","inputs":{"value":"example"}},
    "targets":["test result"],
    "affected_systems":["Biome test"],
    "data_operations":["return supplied value"],
    "expected_effects":["record the supplied test result"],
    "consequences":["the result is retained in Biome"],
    "scope":{"kind":"single"},
    "conditions":[],
    "recovery_actions":[]
  },
  "supporting":{}
}
```

The subprocess first checks active explicit coverage. Otherwise it waits on `human.decision`. It returns `outcome`, `request_id`, and `authorization_id` (empty unless permission was issued). Callers **must branch** on `outcome == "approved"` before a protected action. Other outcomes are `rejected`, `changes_requested`, `deferred`, `clarification_required`, and `authorization_unavailable`. Withdrawn/superseded questions never grant permission.

Default buttons are **Approve**, **Reject**, **Request changes**, and **Defer**. Plain approval of an exact proposal issues permission. Approval accompanied by any textual reply returns `clarification_required`; the original reply is preserved. Present a concrete revised proposal that incorporates the conditions, questions, or requested changes. No language model silently interprets ambiguous prose as permission.

`scope.kind` may be `single`, `group`, or `standing`. Group/standing scope requires an explicit `actions` array of complete `{contract,resource,inputs}` descriptors. Its approval label explicitly says **Approve defined group** or **Approve standing permission**. A group permits each listed action once; standing permission permits repeated matching actions. Full proposal context, targets, effects, revision and conditions must match exactly. Standing permission is never inferred. `expires_at` is optional and explicitly supplied in Unix seconds; no default expiration exists.

## Guard immediately before execution

For direct dispatch invoke:

```json
{"operation":"authorization.dispatch","arguments":{"id":"authorization-id","proposal":{"...":"the exact approved proposal"},"operation":"stable-logical-operation-id"}}
```

The result is the ordinary capability submission/action receipt. Follow `cap.status` with that logical operation ID. The reservation and submission commit atomically. Reusing the same ID reads the same action; it cannot create another reservation. An uncertain result retains its original operation and fence until the provider reconciles it. Never change the ID to retry an uncertain side effect.

A BPMN service task can dispatch and wait through the same guard:

```xml
<serviceTask id="perform" b:contract="example.action:1" b:resource="example.resource"
 b:leaseScope="activity" b:args='{"$":"input.proposal.action.inputs"}'
 b:authorization='{"id":{"$":"permission.authorization_id"},"proposal":{"$":"input.proposal"},"operation":{"$":"input.logical_operation"}}'/>
```

Bind `b` to `urn:biomerte:v1`. Use the actual contract/resource and a unique logical operation per protected activity. The engine waits for the actual provider result and records it in the run trace.

`authorization.protect` opts an exact contract/resource into mandatory guards, also preventing direct `cap.invoke` bypass. The guard checks the grant again when a physical provider claims queued work. A revocation before claim fails that action without invoking the provider. Revoking already-claimed work does not pretend to cancel it: its explicit cancellation or recovery policy applies. Recovery descriptors documented in a proposal are not themselves executable permission; include them explicitly in group/standing `scope.actions` or obtain separate permission.

Conditions are preserved in the grant. Executable conditions use `{description,operation,arguments,expected}`, where operation is a read-only Biome method and expected is its exact output. They are rechecked immediately before submission and provider claim. Unstructured conditions block dispatch until replaced by explicit checks or a revised, approved proposal. They cannot silently be treated as satisfied.

`authorization.get` returns active state and usage records linking the logical operation, exact proposal, original response, question, grant, dispatch state and result. `authorization.revoke` prevents subsequent dispatch; changing a request proposal revision supersedes its old grants. No action-wide or system-wide protection is imposed by default: workflows choose the protected boundaries they actually need.

## Qualification

`tests/test_authorization.py` exercises two unrelated BPMN callers, approval/rejection branches, restart and deduplication, signed phone identity, stale revisions/effects, conditional/free-text replies, cancellation, exact group/standing scope, explicit expiration, atomic single-use races, direct bypass rejection, revocation between reservation and claim, uncertain execution reconciliation, and general decisions that confer no permission. Android tests verify every choice is directly available, locked-phone redaction, pending reply acknowledgement and the required empty state. Physical phone/launcher behavior must also be verified on the installed APK.
