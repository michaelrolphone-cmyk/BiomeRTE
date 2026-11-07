# Native signed provider relay

Source-only change. No deployment, device enrollment, credential generation, access change, service restart or hardware operation is part of this patch.

## Wire contract

POST `/api/provider/call` with JSON:

```
{
  "site": "<controller site>",
  "public_key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
  "envelope": {
    "id": "<unique native request id>",
    "realm": "site",
    "operation": "<native controller operation>",
    "arguments": {},
    "auth": {"actor": "<sha256 SPKI DER hex>", "signature": "<base64 RSA signature>"}
  }
}
```

RSA PKCS#1 v1.5 SHA256 signs native canonical JSON of exactly
`{site,realm,operation,arguments,id}`. The native Python encoding is
`json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)`;
its default `ensure_ascii=True` is significant. The ordinary object-envelope
form supports strings, booleans, null, arrays, objects and safe integer numbers.
For all native numeric spellings and arbitrary Unicode key ordering, callers
can pass `envelope` as the exact serialized native request string and add
`signed_payload` containing the exact canonical signed JSON string. Both are
checked for semantic agreement, and raw request bytes survive forwarding.
The controller independently verifies its own native canonical form.

202 `{pending:true,retry_after_ms:1000}` means persist and retry the exact body.
A lost response, network error or 5xx also means retry that same body. HTTP
requests return promptly rather than waiting for controller execution. A
controller response retains its HTTP status and native `{result:...}` or
`{error:...}` body, with an additional `relay_receipt` on both success and
terminal controller errors. Admission/validation errors have no receipt because
no job was queued. A new read must have a new ID; polling an existing request
returns that request's original result. Device effects still need the native
capability operation IDs and fencing rules. Never invent a new ID after an
uncertain write.

## Explicit result acknowledgement

After durably processing a terminal response, stop `/call` retries and persist
an acknowledgement before submitting the next logical call. POST
`/api/provider/ack`:

```
{"site":"<site>","public_key":"<PEM>","actor":"<fingerprint>",
 "receipt":"native-<64 hex>","signature":"<base64 RSA signature>"}
```

Sign the native Python-canonical encoding of exactly:

```
{"domain":"biomerte-provider-relay-ack-v1","operation":"acknowledge",
 "site":"<site>","actor":"<fingerprint>","receipt":"native-<64 hex>"}
```

This distinct signature cannot be replaced by the original call signature.
Retry the same acknowledgement until HTTP 200
`{acknowledged:true,receipt:"native-..."}`. It deletes the owning actor's
completed result and its chunk rows. Pending/claimed jobs and transient
transport errors reject acknowledgement with 409 and are never deleted.
Already-absent receipts return success without inserting permanent tombstones.
The standard chunk-table foreign-key cascade must be enabled (D1 default).

Acknowledgement is explicitly destructive: it releases gateway replay/result
retention. Do not replay `/call` after acknowledging it, including concurrently
with the acknowledgement. Without permanent tombstones an old call can be
queued again after release; reads may execute again, while the native
controller's actor/request-ID mutation receipts still prevent duplicate writes.
An old acknowledgement can likewise release a later replay of the exact same
call. Clients must serialize this lifecycle and never reuse an acknowledged
request ID for a new logical call. No pending or unacknowledged result expires.

## Integration

1. Review and integrate the gateway worker/lib/build changes into the existing Site.
2. Include `deploy/native_provider.py` alongside each active controller
   connector. Apply `deploy/integration/mac-gateway-agent.patch` and
   `deploy/integration/pi-gateway-agent.patch` to the supplied live connector versions.
   The normal `deploy/gateway-agent.py` includes the same dispatch branch.
   Both active HA connectors must support the new job before admission opens.
3. Configure `BIOME_PROVIDER_ACTORS` as comma-separated device fingerprints.
   An empty value fails closed. This is queue admission only, not enrollment.
   Enroll the matching public key and least-required operation scopes in the
   native controller using its existing authorized commissioning workflow.
4. Build and validate the Site. Deployment and restarts require the parent's
   separately authorized rollout. No schema migration is needed.
5. Verify real HTTPS access from another Wi-Fi network, native enrollment,
   wrong-scope rejection, reconnect, and signed retry after reply loss before
   claiming hardware/network acceptance.

## Security and compatibility

- The public route is not OAuth operator invocation. It cannot choose a local
  URL, supply a bearer token, select an operator identity, enable cached admin
  reads, or invoke MCP through the connector. The connector sends the original
  envelope only to configured loopback `/v1/call`, without Authorization or Site
  credentials, and refuses redirects. Controller signature, actor, enrollment,
  scope, provider ownership and mutation receipt checks remain authoritative.
- The allowlist plus RSA proof stops anonymous/spoofed identities from consuming
  durable queue slots. Body size is capped at 96 KB and pending native jobs at
  40. This is not an edge rate limiter: arbitrary HTTP/crypto load still needs
  the hosting platform's abuse protection. An admitted malicious device can
  consume its shared queue; per-device fairness is not implemented.
- The signed site must match `controller_config.site`. Only `site` realm is
  accepted; arbitrary/local node routing is deliberately unavailable because
  the native signature has no target-node binding. Any ready HA node can run
  the same site request using the shared journal, including peer enrollment.
- There is no bare ID result lookup. Every poll verifies the admitted actor,
  key fingerprint and full RSA proof. Cached result identity includes the full
  original envelope and signed payload, never only the attacker-chosen ID.
  A captured full signed request is replayable and can recover its result.
  TLS and device storage must protect these requests. No freshness window is
  imposed because that would invalidate durable offline retries.
- Controller revocation rejects new execution. Already completed cached results
  do not recheck current controller scopes; remove the actor from the gateway
  allowlist as well when revoking gateway result access. Access to the gateway
  database, operator administration and connector transport remains trusted.
- Native jobs have no deadline and are excluded from ordinary hourly cleanup.
  Existing transport leases still expire and can be reclaimed; those are
  delivery ownership leases, not a job lifetime. Ambiguous failures preserve
  the request ID; native controller actor+ID receipts deduplicate mutations.
  Repeated reads after an ambiguous controller reply may be reexecuted because
  the native controller does not receipt read operations.
- Unacknowledged completed results are retained indefinitely. Explicit signed
  acknowledgement releases them; there is no silent expiry or truncation.
  Clients that abandon results without acknowledging still consume storage.
- Discovery result chunk logic is unchanged. The earlier apparent first-chunk
  failure was the Pi connector pre-framing 470000-byte fetch guard, not the
  chunk protocol. The corrected native Python 3.4.2 regression passed. The
  integration patch preserves that correction. End-to-end validation after
  rollout remains pending; no live hardware/network claim is made here.

## Checks

From `gateway/`: `node --test tests/*.test.mjs`, `npm run build`, and
`npm run validate`. From the repository root: `python3 -m unittest discover
-s tests -p 'test_native_provider.py' -v` (also compatible with Python 3.4.2).

The native tests cover proof rejection, cross-site/origin rejection, unsigned
and extra-field rejection, durable retry and result recovery, new read IDs,
HA reclaim, indefinite retention, bounded admission, controller-denial status,
transport retry, raw numeric bytes, loopback-only routing, redirects and absence
of privileged headers. Acknowledgement tests cover owner, domain, operation,
site and receipt binding, pending/transient rejection, chunk cascade, terminal
error release, repeat acknowledgement without tombstones and post-release
call replay semantics. They use ephemeral test-only keys and no live services.
