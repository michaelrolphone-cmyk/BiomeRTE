# Signed-provider routing in local mode

The gateway transports the device's original signed JSON. It does not sign as the operator, grant scopes, pair a device, or accept cached replies in place of current native authorization. Every HTTP retry creates a fresh transport job; native actor/request receipts still deduplicate effects.

## Existing devices

As part of coordinated local-mode activation, before reopening provider traffic, adopt the verified existing authority into the generic current-ownership registry. Its current rows contain only a responsibility unit, owner, epoch, authority digest and unique routing aliases. `CONTROLLER_NODE_MAP` resolves application identities to enabled registered transports. A temporary `OWNERSHIP_BOOTSTRAP_AUTHORIZATIONS` manifest binds each initial adoption to the SHA-256 of its exact canonical request; remove this manifest after adoption. Do not invent a permanent device placement from fingerprints, physical attachment or an OAuth endpoint.

`adopt_current` registers sole custody already established by the qualified migration manifest. It verifies a bounded exact digest of canonical authority witnesses and their aliases, then persists only retry metadata. It does not copy, pause, replay, complete, or change an active lease/action, and it grants no peer permissions. Its authority digest must bind the singular-owner migration plan and preserved fences; merely finding the same rows in two old mirrors is not that proof. Existing custody continues during a lost response, and exact resume/reconciliation does not rebuild or overwrite its changing domain rows. This path is initial in-place adoption only. `bootstrap` and every subsequent signed transfer retain their quiescent closed-snapshot requirements. The gateway ownership API remains local-mode-only; this is not an enabled pre-cutover shared-mode route.

The existing `PROVIDER_CONTROLLER_MAP` remains a legacy compatibility input, not the new architecture. A stale entry conflicting with current registry ownership is rejected. Receiving controllers still check native signatures, enabled peers, operation scopes and exact resource grants. Routing does not grant authority.

Unchanged devices can keep sending the same six-field signed envelope. The gateway finds the actor's assigned owner and forwards the untouched envelope. Unknown ownership in local mode returns 409 before queueing; it never falls back to `LOCAL_CONTROLLER_ID`. An unavailable owner remains the owner. A different controller cannot claim or complete its transport job.

New clients can include `controller` inside their signed native envelope. Current ownership must agree. Changing the selector without signing again fails native signature verification. An unsigned call query parameter cannot override it. Shared-journal mode with no map or explicit selector retains the previous generic route.

## Finite ownership transfer

`deploy/ownership-control.py` is an explicit operator control plane using the existing registered-controller connection. It does not enroll peers or automatically move a device. Source snapshots are bounded, hash-bound and canonically dependency-checked. Every current executable root requires a stable owner alias. Operational record IDs and payloads belong only to the temporary snapshot, not a permanent shared history.

The source first durably fences the selected responsibility. Unrelated direct writes and scheduler work remain eligible. It signs an exact package for a pinned recipient; the recipient verifies the signature and one-time package approval, checks its exact baseline and stages the package durably. Changed peer/grant authority additionally requires its exact separately approved hashes. Neither side can transfer an active physical action, bound lease or pending activation as though copying its row proved quiescence. Event delivery, local counters and active coordination require separate reconciliation.

Only source freeze plus recipient staging permits registry CAS to the next epoch. A staged recipient may finish after source loss, while the source's durable fence survives restart. This is prepared failover, not reconstruction of unreplicated local writes after an unprepared failure. After local reconciliation, each participant acknowledges; both acknowledgements discard temporary shared scope and transfer metadata. Snapshot payloads are removed locally as soon as their durable reconciliation commits.

Cancellation also advances the epoch while retaining the source owner, so a delayed old prepare cannot resurrect a discarded handoff. An explicit exact-intent resume handles interruption before the first remote write. Expiry never authorizes a takeover or silently clears a fence. Per-controller process-safe serialization prevents staging from racing with cancellation acknowledgement.

The source retains no result payload from moved request receipts. Legacy receipt redirects retain their original bounded retention window; they do not promise unlimited deduplication of arbitrary old IDs. Ordered receipt streams preserve their fixed-slot watermark and reject execution at the old owner. Current ownership metadata is bounded independently from finite transfer admission; exceeding a budget rejects rather than dropping unresolved responsibility.

## Pairing

For an existing device, the gateway canonicalizes its RSA public key as SPKI DER and hashes those bytes, matching the native OpenSSL fingerprint. Begin uses that identity's assigned controller; finish uses the same proof key. The public pair bodies and proof signatures are not rewritten.

For a new unassigned device, an explicit `?controller=<application identity>` can choose the recipient for both begin and finish. This creates only the existing native candidate/proof steps. It does not grant approval or save an ownership assignment. The operator must approve that exact candidate on that same controller using the existing scoped workflow, then qualify its responsibility adoption before an unchanged legacy device can use local-mode calls. Existing current ownership conflicting with the query is rejected.

Moving a route is not a state transfer. Pending candidates, peers, offers, enforcers, actions, leases, fences and provider ledgers must be reconciled together before changing ownership. Do not infer a controller from the provider fingerprint, physical USB attachment, label or default gateway target.

## Link integration

Include `provider_ingress.py` with the controller link. `gateway-agent.py` validates the addressed application identity and removes transport-only metadata before invoking its strict handlers. A signed destination inside the untouched envelope must also match the receiver. Requests go only to configured native loopback endpoints without an operator bearer header; native errors and revocation remain visible.

The helper baseline was recovered from the installed `lowlat-20261104` link with SHA256 `ce622579b47e94a539d3d7a22be0e53ff3585d4c7f8b4741e3e8ebf4ce26a874`. The candidate adds optional signed-destination validation; it does not replace pairing authority or physical-device execution.

## Existing command completion after reattachment

Expiring `provider_dispatch_routes` locate the controller that accepted an action
or fence. MCP acquisition/invocation responses and authenticated native/MCP poll
responses establish these locators before returning work. Action/lease continuations
consult them before current provider placement, including result, claim, custody,
reconciliation and heartbeat/status calls. A conflicting explicit destination is
rejected. New provider polls and registrations still use current placement.

The first recorded controller is immutable for the locator's fixed 48-hour window;
reads and retries do not extend that window or rewrite custody. Locators contain no
command input, output or authorization grant. Missing locators retain compatibility
routing; a locator is not evidence that a native action still exists. The native
controller still verifies the untouched signed envelope and current authorization
on every retry. An unavailable original controller never authorizes result failover.
