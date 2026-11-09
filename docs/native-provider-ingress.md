# Native mobile providers over the existing Biome gateway

This ingress allows a Wi-Fi device to participate with its own scoped native
BiomeRTE key. The device needs only an outbound verified HTTPS connection to the
existing controller Site. It does not use a USB runtime connection, an owner
OAuth credential, or the old Channel Bus.

## Commissioning

The device generates an RSA-2048 key and preserves the private key locally.
Its actor ID is the SHA-256 digest of SubjectPublicKeyInfo DER for the public key,
matching `biomerte.identity.fingerprint`.

1. POST `/api/provider/pair/begin` with `{public_key,label}`.
2. The native response is `{result:{challenge,site,key}}`. Preserve that exact
   challenge. Independently compare `key` to the physical device identity.
3. The owner calls the existing `pair.approve` operation with that fingerprint
   and exact flat scopes. The public route cannot approve or choose scopes.
4. POST `/api/provider/pair/finish` with `{challenge,site,key,signature}`.
   Sign the canonical `{challenge,site,key}` object. An unapproved candidate
   returns HTTP 403; retry the same finish proof after approval.
5. A successful reply is `{result:{site,credential,scopes}}`. Repeated finish
   requests recover the same enrolled identity without making another candidate.

The relay provider needs `cap.offer`, `provider.poll`, `cap.fenced`,
`provider.claim`, `provider.result`, `cap.status`, an `offer:CONTRACT`
entry for every one of its five physical contracts, and
`resource:relay6ch.<device-MAC>`. Contracts are defined by the owner before
the device advertises. Capability names remain generic.

## Signed calls

POST `/api/provider/call` with:

```json
{"site":"SITE","realm":"site","id":"REQUEST-ID","operation":"provider.poll","arguments":{"provider":"PUBLIC-KEY-FINGERPRINT","limit":1},"auth":{"actor":"PUBLIC-KEY-FINGERPRINT","signature":"BASE64-RSA-SHA256"}}
```

The signature uses RSA PKCS#1 v1.5 SHA-256 over UTF-8 canonical JSON of
`{site,realm,id,operation,arguments}`, without `auth`. Canonical encoding is
`json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)`,
including Python's default ASCII escaping. Successful replies use the native
`{result:...}` wrapper; rejection preserves the native HTTP status and
`{error:...}` body.

The public endpoint admits provider operations only. Each call is forwarded as
the **original JSON text** to `/v1/call` on the selected active controller,
without any operator bearer header. The native handler verifies the signature,
current peer revocation state, granted operation scopes, provider identity,
resource ownership and command binding. A newly queued bridge request is used
even for repeated request IDs; the gateway cannot serve a previously authorized
result after revocation. The native controller still deduplicates mutations
using the original signed ID. JavaScript never rewrites the signed numbers.

A transport error or 503 means repeat the same logical request ID and body.
It does not authorize repeating physical effects or creating another command.
No physical operation duration limit is added by this transport.

## Small device inbox pages

`provider.poll` now accepts optional positive `limit`. Default behavior
remains the existing maximum of 64 returned actions. A device with one physical
resource requests `limit:1`. Queued work stays on the controller; completed
actions leave the outstanding inbox, exposing the next one. Fences and
activations retain their existing shapes. This is response sizing, not a limit
on stored commands or their execution duration.

## Deployment and checks

Deploy the gateway source addition `gateway/lib/provider.mjs`, the route in
`gateway/worker/index.js`, the body reader in `gateway/lib/bridge.mjs`, and
updated build script together. Update both physical controller checkouts with
`deploy/provider_ingress.py`, `deploy/gateway-agent.py` and
`biomerte/providers.py`; restart each controller and outbound link using
their established deployment procedure. Both members must understand the new
transport before failover qualification.

Run from the BiomeRTE checkout:

```text
python3 -m unittest tests.test_provider_ingress tests.test_lease_queue tests.test_gateway_discovery
```

Run from its `gateway` directory using the existing Node 24 runtime:

```text
node --test tests/*.test.mjs
```

Tests cover actual native RSA verification, signature tampering, resource
scope denial, revocation of a previously successful request, pairing approval,
lost-reply-safe repeat finish, response paging without lost commands, the lack
of operator credentials on forwarding requests, and preservation of signed
JSON numbers through the gateway.
